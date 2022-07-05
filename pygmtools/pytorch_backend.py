import itertools
import torch
import numpy as np
from multiprocessing import Pool
from torch import Tensor

import pygmtools.utils
from pygmtools.numpy_backend import _hung_kernel


#############################################
#     Linear Assignment Problem Solvers     #
#############################################


def hungarian(s: Tensor, n1: Tensor=None, n2: Tensor=None, nproc: int=1) -> Tensor:
    """
    Pytorch implementation of Hungarian algorithm
    """
    device = s.device
    batch_num = s.shape[0]

    perm_mat = s.cpu().detach().numpy() * -1
    if n1 is not None:
        n1 = n1.cpu().numpy()
    else:
        n1 = [None] * batch_num
    if n2 is not None:
        n2 = n2.cpu().numpy()
    else:
        n2 = [None] * batch_num

    if nproc > 1:
        with Pool(processes=nproc) as pool:
            mapresult = pool.starmap_async(_hung_kernel, zip(perm_mat, n1, n2))
            perm_mat = np.stack(mapresult.get())
    else:
        perm_mat = np.stack([_hung_kernel(perm_mat[b], n1[b], n2[b]) for b in range(batch_num)])

    perm_mat = torch.from_numpy(perm_mat).to(device)

    return perm_mat


def sinkhorn(s: Tensor, nrows: Tensor=None, ncols: Tensor=None,
             dummy_row: bool=False, max_iter: int=10, tau: float=1., batched_operation: bool=False) -> Tensor:
    """
    Pytorch implementation of Sinkhorn algorithm
    """
    batch_size = s.shape[0]

    if s.shape[2] >= s.shape[1]:
        transposed = False
    else:
        s = s.transpose(1, 2)
        nrows, ncols = ncols, nrows
        transposed = True

    if nrows is None:
        nrows = [s.shape[1] for _ in range(batch_size)]
    if ncols is None:
        ncols = [s.shape[2] for _ in range(batch_size)]

    # operations are performed on log_s
    s = s / tau

    if dummy_row:
        assert s.shape[2] >= s.shape[1]
        dummy_shape = list(s.shape)
        dummy_shape[1] = s.shape[2] - s.shape[1]
        ori_nrows = nrows
        nrows = ncols
        s = torch.cat((s, torch.full(dummy_shape, -float('inf')).to(s.device)), dim=1)
        for b in range(batch_size):
            s[b, ori_nrows[b]:nrows[b], :ncols[b]] = -100
            s[b, nrows[b]:, :] = -float('inf')
            s[b, :, ncols[b]:] = -float('inf')

    if batched_operation:
        log_s = s

        for i in range(max_iter):
            if i % 2 == 0:
                log_sum = torch.logsumexp(log_s, 2, keepdim=True)
                log_s = log_s - log_sum
                log_s[torch.isnan(log_s)] = -float('inf')
            else:
                log_sum = torch.logsumexp(log_s, 1, keepdim=True)
                log_s = log_s - log_sum
                log_s[torch.isnan(log_s)] = -float('inf')

        if dummy_row and dummy_shape[1] > 0:
            log_s = log_s[:, :-dummy_shape[1]]
            for b in range(batch_size):
                log_s[b, ori_nrows[b]:nrows[b], :ncols[b]] = -float('inf')

        if transposed:
            log_s = log_s.transpose(1, 2)

        return torch.exp(log_s)
    else:
        ret_log_s = torch.full((batch_size, s.shape[1], s.shape[2]), -float('inf'), device=s.device, dtype=s.dtype)

        for b in range(batch_size):
            row_slice = slice(0, nrows[b])
            col_slice = slice(0, ncols[b])
            log_s = s[b, row_slice, col_slice]

            for i in range(max_iter):
                if i % 2 == 0:
                    log_sum = torch.logsumexp(log_s, 1, keepdim=True)
                    log_s = log_s - log_sum
                else:
                    log_sum = torch.logsumexp(log_s, 0, keepdim=True)
                    log_s = log_s - log_sum

            ret_log_s[b, row_slice, col_slice] = log_s

        if dummy_row:
            if dummy_shape[1] > 0:
                ret_log_s = ret_log_s[:, :-dummy_shape[1]]
            for b in range(batch_size):
                ret_log_s[b, ori_nrows[b]:nrows[b], :ncols[b]] = -float('inf')

        if transposed:
            ret_log_s = ret_log_s.transpose(1, 2)

        return torch.exp(ret_log_s)


#############################################
#    Quadratic Assignment Problem Solvers   #
#############################################


def rrwm(K: Tensor, n1: Tensor, n2: Tensor, n1max, n2max, x0: Tensor,
         max_iter: int, sk_iter: int, alpha: float, beta: float) -> Tensor:
    """
    Pytorch implementation of RRWM algorithm.
    """
    batch_num, n1, n2, n1max, n2max, n1n2, v0 = _check_and_init_gm(K, n1, n2, n1max, n2max, x0)
    # rescale the values in K
    d = K.sum(dim=2, keepdim=True)
    dmax = d.max(dim=1, keepdim=True).values
    K = K / (dmax + d.min() * 1e-5)
    v = v0
    for i in range(max_iter):
        # random walk
        v = torch.bmm(K, v)
        last_v = v
        n = torch.norm(v, p=1, dim=1, keepdim=True)
        v = v / n

        # reweighted jump
        s = v.view(batch_num, n2max, n1max).transpose(1, 2)
        s = beta * s / s.max(dim=1, keepdim=True).values.max(dim=2, keepdim=True).values
        v = alpha * sinkhorn(s, n1, n2, max_iter=sk_iter).transpose(1, 2).reshape(batch_num, n1n2, 1) + \
            (1 - alpha) * v
        n = torch.norm(v, p=1, dim=1, keepdim=True)
        v = torch.matmul(v, 1 / n)

        if torch.norm(v - last_v) < 1e-5:
            break

    return v.view(batch_num, n2max, n1max).transpose(1, 2)


def sm(K: Tensor, n1: Tensor, n2: Tensor, n1max, n2max, x0: Tensor,
       max_iter: int) -> Tensor:
    """
    Pytorch implementation of SM algorithm.
    """
    batch_num, n1, n2, n1max, n2max, n1n2, v0 = _check_and_init_gm(K, n1, n2, n1max, n2max, x0)
    v = vlast = v0
    for i in range(max_iter):
        v = torch.bmm(K, v)
        n = torch.norm(v, p=2, dim=1)
        v = torch.matmul(v, (1 / n).view(batch_num, 1, 1))
        if torch.norm(v - vlast) < 1e-5:
            break
        vlast = v

    x = v.view(batch_num, n2max, n1max).transpose(1, 2)
    return x

def ipfp(K: Tensor, n1: Tensor, n2: Tensor, n1max, n2max, x0: Tensor,
         max_iter) -> Tensor:
    """
    Pytorch implementation of IPFP algorithm
    """
    batch_num, n1, n2, n1max, n2max, n1n2, v0 = _check_and_init_gm(K, n1, n2, n1max, n2max, x0)
    v = v0
    last_v = v

    def comp_obj_score(v1, K, v2):
        return torch.bmm(torch.bmm(v1.view(batch_num, 1, -1), K), v2)

    for i in range(max_iter):
        cost = torch.bmm(K, v).reshape(batch_num, n2max, n1max).transpose(1, 2)
        binary_sol = hungarian(cost, n1, n2)
        binary_v = binary_sol.transpose(1, 2).view(batch_num, -1, 1)
        alpha = comp_obj_score(v, K, binary_v - v)  # + torch.mm(k_diag.view(1, -1), (binary_sol - v).view(-1, 1))
        beta = comp_obj_score(binary_v - v, K, binary_v - v)
        t0 = alpha / beta
        v = torch.where(torch.logical_or(beta <= 0, t0 >= 1), binary_v, v + t0 * (binary_v - v))
        last_v_sol = comp_obj_score(last_v, K, last_v)
        if torch.max(torch.abs(
                last_v_sol - torch.bmm(cost.reshape(batch_num, 1, -1), binary_sol.reshape(batch_num, -1, 1))
        ) / last_v_sol) < 1e-3:
            break
        last_v = v

    pred_x = binary_sol
    return pred_x


def _check_and_init_gm(K, n1, n2, n1max, n2max, x0):
    # get batch number
    batch_num = K.shape[0]
    n1n2 = K.shape[1]

    # get values of n1, n2, n1max, n2max and check
    if n1 is None:
        n1 = torch.full((batch_num,), n1max, dtype=torch.int, device=K.device)
    if n2 is None:
        n2 = torch.full((batch_num,), n2max, dtype=torch.int, device=K.device)
    if n1max is None:
        n1max = torch.max(n1)
    if n2max is None:
        n2max = torch.max(n2)

    assert n1max * n2max == n1n2, 'the input size of K does not match with n1max * n2max!'

    # initialize x0 (also v0)
    if x0 is None:
        x0 = torch.zeros(batch_num, n1max, n2max, dtype=K.dtype, device=K.device)
        for b in range(batch_num):
            x0[b, 0:n1[b], 0:n2[b]] = torch.tensor(1.) / (n1[b] * n2[b])
    v0 = x0.transpose(1, 2).reshape(batch_num, n1n2, 1)

    return batch_num, n1, n2, n1max, n2max, n1n2, v0


############################################
#      Multi-Graph Matching Solvers        #
############################################


def cao_solver(K, X, num_graph, num_node, max_iter, lambda_init, lambda_step, lambda_max, iter_boost):
    r"""
    Pytorch implementation of CAO solver (mode="c")

    :param K: affinity matrix, (m, m, n*n, n*n)
    :param X: initial matching, (m, m, n, n)
    :param num_graph: number of graphs, int
    :param num_node: number of nodes, int
    :return: X, (m, m, n, n)
    """
    m, n = num_graph, num_node
    param_lambda = lambda_init
    device = K.device

    for iter in range(max_iter):
        if iter >= iter_boost:
            param_lambda = np.min([param_lambda * lambda_step, lambda_max])
        # pair_con = get_batch_pc_opt(X)
        pair_aff = pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch').reshape(m, m)
        pair_aff = pair_aff - torch.eye(m, device=device) * pair_aff
        norm = torch.max(pair_aff)
        for i in range(m):
            for j in range(m):
                if i >= j:
                    continue
                aff_ori = pygmtools.utils.compute_affinity_score(X[i, j], K[i, j], backend='pytorch') / norm
                con_ori = _get_single_pc_opt(X, i, j)
                # con_ori = torch.sqrt(pair_con[i, j])
                if iter < iter_boost:
                    score_ori = aff_ori
                else:
                    score_ori = aff_ori * (1 - param_lambda) + con_ori * param_lambda
                X_upt = X[i, j]
                for k in range(m):
                    X_combo = torch.matmul(X[i, k], X[k, j])
                    aff_combo = pygmtools.utils.compute_affinity_score(X_combo, K[i, j], backend='pytorch') / norm
                    con_combo = _get_single_pc_opt(X, i, j, X_combo)
                    # con_combo = torch.sqrt(pair_con[i, k] * pair_con[k, j])
                    if iter < iter_boost:
                        score_combo = aff_combo
                    else:
                        score_combo = aff_combo * (1 - param_lambda) + con_combo * param_lambda
                    if score_combo > score_ori:
                        X_upt = X_combo
                X[i, j] = X_upt
                X[j, i] = X_upt.transpose(0, 1)
    return X


def cao_fast_solver(K, X, num_graph, num_node, max_iter, lambda_init, lambda_step, lambda_max, iter_boost):
    r"""
    Pytorch implementation of CAO solver in fast config (mode="pc")

    :param K: affinity matrix, (m, m, n*n, n*n)
    :param X: initial matching, (m, m, n, n)
    :param num_graph: number of graphs, int
    :param num_node: number of nodes, int
    :return: X, (m, m, n, n)
    """
    m, n = num_graph, num_node
    param_lambda = lambda_init

    device = K.device
    mask1 = torch.arange(m).reshape(m, 1).repeat(1, m).to(device)
    mask2 = torch.arange(m).reshape(1, m).repeat(m, 1).to(device)
    mask = (mask1 < mask2).float()
    X_mask = mask.reshape(m, m, 1, 1)

    for iter in range(max_iter):
        if iter >= iter_boost:
            param_lambda = np.min([param_lambda * lambda_step, lambda_max])

        pair_aff = pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch').reshape(m, m)
        pair_aff = pair_aff - torch.eye(m, device=device) * pair_aff
        norm = torch.max(pair_aff)

        X1 = X.reshape(m, 1, m, n, n).repeat(1, m, 1, 1, 1).reshape(-1, n, n)  # X1[i,j,k] = X[i,k]
        X2 = X.reshape(1, m, m, n, n).repeat(m, 1, 1, 1, 1).transpose(1, 2).reshape(-1, n, n)  # X2[i,j,k] = X[k,j]
        X_combo = torch.bmm(X1, X2).reshape(m, m, m, n, n) # X_combo[i,j,k] = X[i, k] * X[k, j]

        aff_ori = (pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n)) / norm).reshape(m, m)
        pair_con = _get_batch_pc_opt(X)
        con_ori = torch.sqrt(pair_con)

        K_repeat = K.reshape(m, m, 1, n * n, n * n).repeat(1, 1, m, 1, 1).reshape(-1, n * n, n * n)
        aff_combo = (pygmtools.utils.compute_affinity_score(X_combo.reshape(-1, n, n), K_repeat) / norm).reshape(m, m, m)
        con1 = pair_con.reshape(m, 1, m).repeat(1, m, 1)  # con1[i,j,k] = pair_con[i,k]
        con2 = pair_con.reshape(1, m, m).repeat(m, 1, 1).transpose(1, 2)  # con2[i,j,k] = pair_con[j,k]
        con_combo = torch.sqrt(con1 * con2)

        if iter < iter_boost:
            score_ori = aff_ori
            score_combo = aff_combo
        else:
            score_ori = aff_ori * (1 - param_lambda) + con_ori * param_lambda
            score_combo = aff_combo * (1 - param_lambda) + con_combo * param_lambda

        score_combo, idx = torch.max(score_combo, dim=-1)

        assert torch.all(score_combo >= score_ori), torch.min(score_combo - score_ori)
        X_upt = X_combo[mask1, mask2, idx, :, :]
        X = X_upt * X_mask + X_upt.transpose(0, 1).transpose(2, 3) * X_mask.transpose(0, 1) + X * (1 - X_mask - X_mask.transpose(0, 1))
        assert torch.all(X.transpose(0, 1).transpose(2, 3) == X)
    return X


def mgm_floyd_solver(K, X, num_graph, num_node, param_lambda):
    m, n = num_graph, num_node
    device = K.device

    for k in range(m):
        pair_aff = pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch').reshape(m, m)
        pair_aff = pair_aff - torch.eye(m, device=device) * pair_aff
        norm = torch.max(pair_aff)

        # print("iter:{} aff:{:.4f} con:{:.4f}".format(
        #     k, torch.mean(pair_aff).item(), torch.mean(get_batch_pc_opt(X)).item()
        # ))

        for i in range(m):
            for j in range(m):
                if i >= j:
                    continue
                score_ori = pygmtools.utils.compute_affinity_score(X[i, j], K[i, j], backend='pytorch') / norm
                X_combo = torch.matmul(X[i, k], X[k, j])
                score_combo = pygmtools.utils.compute_affinity_score(X_combo, K[i, j], backend='pytorch') / norm

                if score_combo > score_ori:
                    X[i, j] = X_combo
                    X[j, i] = X_combo.transpose(0, 1)

    for k in range(m):
        pair_aff = pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch').reshape(m, m)
        pair_aff = pair_aff - torch.eye(m, device=device) * pair_aff
        norm = torch.max(pair_aff)

        pair_con = _get_batch_pc_opt(X)
        for i in range(m):
            for j in range(m):
                if i >= j:
                    continue
                aff_ori = pygmtools.utils.compute_affinity_score(X[i, j], K[i, j], backend='pytorch') / norm
                con_ori = _get_single_pc_opt(X, i, j)
                # con_ori = torch.sqrt(pair_con[i, j])
                score_ori = aff_ori * (1 - param_lambda) + con_ori * param_lambda

                X_combo = torch.matmul(X[i, k], X[k, j])
                aff_combo = pygmtools.utils.compute_affinity_score(X_combo, K[i, j], backend='pytorch') / norm
                con_combo = _get_single_pc_opt(X, i, j, X_combo)
                # con_combo = torch.sqrt(pair_con[i, k] * pair_con[k, j])
                score_combo = aff_combo * (1 - param_lambda) + con_combo * param_lambda

                if score_combo > score_ori:
                    X[i, j] = X_combo
                    X[j, i] = X_combo.transpose(0, 1)
    return X


def mgm_floyd_fast_solver(K, X, num_graph, num_node, param_lambda):
    m, n = num_graph, num_node
    device = K.device

    mask1 = torch.arange(m).reshape(m, 1).repeat(1, m)
    mask2 = torch.arange(m).reshape(1, m).repeat(m, 1)
    mask = (mask1 < mask2).float().to(device)
    X_mask = mask.reshape(m, m, 1, 1)

    for k in range(m):
        pair_aff = pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch').reshape(m, m)
        pair_aff = pair_aff - torch.eye(m, device=device) * pair_aff
        norm = torch.max(pair_aff)

        # print("iter:{} aff:{:.4f} con:{:.4f}".format(
        #     k, torch.mean(pair_aff).item(), torch.mean(get_batch_pc_opt(X)).item()
        # ))

        X1 = X[:, k].reshape(m, 1, n, n).repeat(1, m, 1, 1).reshape(-1, n, n)  # X[i, j] = X[i, k]
        X2 = X[k, :].reshape(1, m, n, n).repeat(m, 1, 1, 1).reshape(-1, n, n)  # X[i, j] = X[j, k]
        X_combo = torch.bmm(X1, X2).reshape(m, m, n, n)

        aff_ori = (pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch') / norm).reshape(m, m)
        aff_combo = (pygmtools.utils.compute_affinity_score(X_combo.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch') / norm).reshape(m, m)

        score_ori = aff_ori
        score_combo = aff_combo

        upt = (score_ori < score_combo).float()
        upt = (upt * mask).reshape(m, m, 1, 1)
        X = X * (1.0 - upt) + X_combo * upt
        X = X * X_mask + X.transpose(0, 1).transpose(2, 3) * (1 - X_mask)

    for k in range(m):
        pair_aff = pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch').reshape(m, m)
        pair_aff = pair_aff - torch.eye(m, device=device) * pair_aff
        norm = torch.max(pair_aff)

        pair_con = _get_batch_pc_opt(X)

        X1 = X[:, k].reshape(m, 1, n, n).repeat(1, m, 1, 1).reshape(-1, n, n)  # X[i, j] = X[i, k]
        X2 = X[k, :].reshape(1, m, n, n).repeat(m, 1, 1, 1).reshape(-1, n, n)  # X[i, j] = X[j, k]
        X_combo = torch.bmm(X1, X2).reshape(m, m, n, n)

        aff_ori = (pygmtools.utils.compute_affinity_score(X.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch') / norm).reshape(m, m)
        aff_combo = (pygmtools.utils.compute_affinity_score(X_combo.reshape(-1, n, n), K.reshape(-1, n * n, n * n), backend='pytorch') / norm).reshape(m, m)

        con_ori = torch.sqrt(pair_con)
        con1 = pair_con[:, k].reshape(m, 1).repeat(1, m)
        con2 = pair_con[k, :].reshape(1, m).repeat(m, 1)
        con_combo = torch.sqrt(con1 * con2)

        score_ori = aff_ori * (1 - param_lambda) + con_ori * param_lambda
        score_combo = aff_combo * (1 - param_lambda) + con_combo * param_lambda

        upt = (score_ori < score_combo).float()
        upt = (upt * mask).reshape(m, m, 1, 1)
        X = X * (1.0 - upt) + X_combo * upt
        X = X * X_mask + X.transpose(0, 1).transpose(2, 3) * (1 - X_mask)
    return X


def _get_single_pc_opt(X, i, j, Xij=None):
    """
    CAO/Floyd helper function (compute consistency)
    :param X: (m, m, n, n) all the matching results
    :param i: index
    :param j: index
    :return: the consistency of X_ij
    """
    m, _, n, _ = X.size()
    if Xij is None:
        Xij = X[i, j]
    X1 = X[i, :].reshape(-1, n, n)
    X2 = X[:, j].reshape(-1, n, n)
    X_combo = torch.bmm(X1, X2)
    pair_con = 1 - torch.sum(torch.abs(Xij - X_combo)) / (2 * n * m)
    return pair_con


def _get_batch_pc_opt(X):
    """
    CAO/Floyd-fast helper function (compute consistency in batch)
    :param X: (m, m, n, n) all the matching results
    :return: (m, m) the consistency of X
    """
    m, _, n, _ = X.size()
    X1 = X.reshape(m, 1, m, n, n).repeat(1, m, 1, 1, 1).reshape(-1, n, n)  # X1[i, j, k] = X[i, k]
    X2 = X.reshape(1, m, m, n, n).repeat(m, 1, 1, 1, 1).transpose(1, 2).reshape(-1, n, n)  # X2[i, j, k] = X[k, j]
    X_combo = torch.bmm(X1, X2).reshape(m, m, m, n, n)
    X_ori = X.reshape(m, m, 1, n, n).repeat(1, 1, m, 1, 1)
    pair_con = 1 - torch.sum(torch.abs(X_combo - X_ori), dim=(2, 3, 4)) / (2 * n * m)
    return pair_con


def gamgm(A, W, ns, n_univ, U0, init_tau, min_tau, sk_gamma, sk_iter, max_iter, quad_weight, converge_thresh, verbose,
          cluster_M=None, projector='sinkhorn', hung_iter=True # these arguments are reserved for clustering
          ):
    """
    Pytorch implementation of Graduated Assignment for Multi-Graph Matching (with compatibility for 2GM and clustering)
    """
    num_graphs = A.shape[0]
    if ns is None:
        ns = torch.full((num_graphs,), A.shape[1], dtype=torch.int, device=A.device)
    n_indices = torch.cumsum(ns, dim=0)

    # build a super adjacency matrix A
    supA = torch.zeros(n_indices[-1], n_indices[-1], device=A.device)
    for i in range(num_graphs):
        start_n = n_indices[i] - ns[i]
        end_n = n_indices[i]
        supA[start_n:end_n, start_n:end_n] = A[i, :ns[i], :ns[i]]

    # handle the type of n_univ
    if type(n_univ) is torch.Tensor:
        n_univ = n_univ.item()

    # randomly init U
    if U0 is None:
        U0 = torch.full((n_indices[-1], n_univ), 1 / n_univ, device=A.device)
        U0 += torch.randn_like(U0) / 1000

    # init cluster_M if not given
    if cluster_M is None:
        cluster_M = torch.ones(num_graphs, num_graphs, device=A.device)

    # reshape W into supW
    supW = torch.zeros(n_indices[-1], n_indices[-1], device=A.device)
    for i, j in itertools.product(range(num_graphs), repeat=2):
        start_x = n_indices[i] - ns[i]
        end_x = n_indices[i]
        start_y = n_indices[j] - ns[j]
        end_y = n_indices[j]
        supW[start_x:end_x, start_y:end_y] = W[i, j, :ns[i], :ns[j]]

    U = U0
    sinkhorn_tau = init_tau
    iter_flag = True

    while iter_flag:
        for i in range(max_iter):
            # compact matrix form update of V
            UUt = torch.mm(U, U.t())
            lastUUt = UUt
            cluster_weight = torch.repeat_interleave(cluster_M, ns.to(dtype=torch.long), dim=0)
            cluster_weight = torch.repeat_interleave(cluster_weight, ns.to(dtype=torch.long), dim=1)
            V = torch.chain_matmul(supA, UUt * cluster_weight, supA, U) * quad_weight * 2 + torch.mm(supW * cluster_weight, U)
            V /= num_graphs

            U_list = []
            if projector == 'hungarian':
                n_start = 0
                for n_end in n_indices:
                    U_list.append(pygmtools.hungarian(V[n_start:n_end, :n_univ], backend='pytorch'))
                    n_start = n_end
            elif projector == 'sinkhorn':
                if torch.all(ns == ns[0]):
                    if ns[0] <= n_univ:
                        U_list.append(
                            sinkhorn(
                                V.reshape(num_graphs, -1, n_univ),
                                max_iter=sk_iter, tau=sinkhorn_tau, batched_operation=True, dummy_row=True
                            ).reshape(-1, n_univ))
                    else:
                        U_list.append(
                            sinkhorn(
                                V.reshape(num_graphs, -1, n_univ).transpose(1, 2),
                                max_iter=sk_iter, tau=sinkhorn_tau, batched_operation=True, dummy_row=True
                            ).transpose(1, 2).reshape(-1, n_univ))
                else:
                    V_list = []
                    n1 = []
                    n_start = 0
                    for n_end in n_indices:
                        V_list.append(V[n_start:n_end, :n_univ])
                        n1.append(n_end - n_start)
                        n_start = n_end
                    n1 = torch.tensor(n1)
                    U = sinkhorn(build_batch(V_list), n1,
                                 max_iter=sk_iter, tau=sinkhorn_tau, batched_operation=True, dummy_row=True)
                    n_start = 0
                    for idx, n_end in enumerate(n_indices):
                        U_list.append(U[idx, :n_end - n_start, :])
                        n_start = n_end
            else:
                raise NameError('Unknown projecter name: {}'.format(projector))

            U = torch.cat(U_list, dim=0)
            if num_graphs == 2:
                U[:ns[0], :] = torch.eye(ns[0], n_univ, device=U.device)

            if torch.norm(torch.mm(U, U.t()) - lastUUt) < converge_thresh:
                break

        if i == max_iter - 1: # not converged
            if hung_iter:
                pass
            else:
                U_list = [pygmtools.hungarian(_, backend='pytorch') for _ in U_list]
                U = torch.cat(U_list, dim=0)
                if verbose: print(i, 'max_iter')
                break

        # projection control
        if projector == 'hungarian':
            if verbose: print(i, 'hungarian')
            break
        elif sinkhorn_tau > min_tau:
            if verbose: print(i, 'tau=', sinkhorn_tau)
            sinkhorn_tau *= sk_gamma
        else:
            if hung_iter:
                projector = 'hungarian'
            else:
                U_list = [pygmtools.hungarian(_, backend='pytorch') for _ in U_list]
                U = torch.cat(U_list, dim=0)
                break

    # return result
    result = pygmtools.utils.MultiMatchingResult(True, 'pytorch')
    for i in range(num_graphs):
        start_n = n_indices[i] - ns[i]
        end_n = n_indices[i]
        result[i] = U[start_n:end_n]
    return result


#############################################
#              Utils Functions              #
#############################################


def inner_prod_aff_fn(feat1, feat2):
    """
    Pytorch implementation of inner product affinity function
    """
    return torch.matmul(feat1, feat2.transpose(1, 2))


def gaussian_aff_fn(feat1, feat2, sigma):
    """
    Pytorch implementation of Gaussian affinity function
    """
    feat1 = feat1.unsqueeze(2)
    feat2 = feat2.unsqueeze(1)
    return torch.exp(-((feat1 - feat2) ** 2).sum(dim=-1) / sigma)


def build_batch(input, return_ori_dim=False):
    """
    Pytorch implementation of building a batched tensor
    """
    assert type(input[0]) == torch.Tensor
    device = input[0].device
    it = iter(input)
    t = next(it)
    max_shape = list(t.shape)
    ori_shape = [[_] for _ in max_shape]
    while True:
        try:
            t = next(it)
            for i in range(len(max_shape)):
                max_shape[i] = int(max(max_shape[i], t.shape[i]))
                ori_shape[i].append(t.shape[i])
        except StopIteration:
            break
    max_shape = np.array(max_shape)

    padded_ts = []
    for t in input:
        pad_pattern = np.zeros(2 * len(max_shape), dtype=np.int64)
        pad_pattern[::-2] = max_shape - np.array(t.shape)
        pad_pattern = tuple(pad_pattern.tolist())
        padded_ts.append(torch.nn.functional.pad(t, pad_pattern, 'constant', 0))

    if return_ori_dim:
        return torch.stack(padded_ts, dim=0), tuple([torch.tensor(_, dtype=torch.int64, device=device) for _ in ori_shape])
    else:
        return torch.stack(padded_ts, dim=0)

def dense_to_sparse(dense_adj):
    """
    Pytorch implementation of converting a dense adjacency matrix to a sparse matrix
    """
    batch_size = dense_adj.shape[0]
    conn, ori_shape = build_batch([torch.nonzero(a, as_tuple=False) for a in dense_adj], return_ori_dim=True)
    nedges = ori_shape[0]
    edge_weight = build_batch([dense_adj[b][(conn[b, :, 0], conn[b, :, 1])] for b in range(batch_size)])
    return conn, edge_weight.unsqueeze(-1), nedges


def compute_affinity_score(X, K):
    """
    Pytorch implementation of computing affinity score
    """
    b, n, _ = X.size()
    vx = X.transpose(1, 2).reshape(b, -1, 1)  # (b, n*n, 1)
    vxt = vx.transpose(1, 2)  # (b, 1, n*n)
    affinity = torch.bmm(torch.bmm(vxt, K), vx)
    return affinity


def to_numpy(input):
    """
    Pytorch function to_numpy
    """
    return input.detach().cpu().numpy()


def from_numpy(input, device):
    """
    Pytorch function from_numpy
    """
    if device is None:
        return torch.from_numpy(input)
    else:
        return torch.from_numpy(input).to(device)


def generate_isomorphic_graphs(node_num, graph_num, node_feat_dim):
    """
    Pytorch implementation of generate_isomorphic_graphs
    """
    X_gt = torch.zeros(graph_num, node_num, node_num)
    X_gt[0, torch.arange(0, node_num, dtype=torch.int64), torch.arange(0, node_num, dtype=torch.int64)] = 1
    for i in range(graph_num):
        if i > 0:
            X_gt[i, torch.arange(0, node_num, dtype=torch.int64), torch.randperm(node_num)] = 1
    joint_X = X_gt.reshape(graph_num * node_num, node_num)
    X_gt = torch.mm(joint_X, joint_X.t())
    X_gt = X_gt.reshape(graph_num, node_num, graph_num, node_num).permute(0, 2, 1, 3)
    A0 = torch.rand(node_num, node_num)
    torch.diagonal(A0)[:] = 0
    As = [A0]
    for i in range(graph_num):
        if i > 0:
            As.append(torch.mm(torch.mm(X_gt[i, 0], A0), X_gt[0, i]))
    if node_feat_dim > 0:
        F0 = torch.rand(node_num, node_feat_dim)
        Fs = [F0]
        for i in range(graph_num):
            if i > 0:
                Fs.append(torch.mm(X_gt[i, 0], F0))
        return torch.stack(As, dim=0), X_gt, torch.stack(Fs, dim=0)
    else:
        return torch.stack(As, dim=0), X_gt


def _aff_mat_from_node_edge_aff(node_aff: Tensor, edge_aff: Tensor, connectivity1: Tensor, connectivity2: Tensor,
                                n1, n2, ne1, ne2):
    """
    Pytorch implementation of _aff_mat_from_node_edge_aff
    """
    if edge_aff is not None:
        device = edge_aff.device
        dtype = edge_aff.dtype
        batch_size = edge_aff.shape[0]
        if n1 is None:
            n1 = torch.max(torch.max(connectivity1, dim=-1).values, dim=-1).values + 1
        if n2 is None:
            n2 = torch.max(torch.max(connectivity2, dim=-1).values, dim=-1).values + 1
        if ne1 is None:
            ne1 = [edge_aff.shape[1]] * batch_size
        if ne2 is None:
            ne2 = [edge_aff.shape[1]] * batch_size
    else:
        device = node_aff.device
        dtype = node_aff.dtype
        batch_size = node_aff.shape[0]
        if n1 is None:
            n1 = [node_aff.shape[1]] * batch_size
        if n2 is None:
            n2 = [node_aff.shape[2]] * batch_size

    n1max = max(n1)
    n2max = max(n2)
    ks = []
    for b in range(batch_size):
        k = torch.zeros(n2max, n1max, n2max, n1max, dtype=dtype, device=device)
        # edge-wise affinity
        if edge_aff is not None:
            conn1 = connectivity1[b][:ne1[b]]
            conn2 = connectivity2[b][:ne2[b]]
            edge_indices = torch.cat([conn1.repeat_interleave(ne2[b], dim=0), conn2.repeat(ne1[b], 1)], dim=1) # indices: start_g1, end_g1, start_g2, end_g2
            edge_indices = (edge_indices[:, 2], edge_indices[:, 0], edge_indices[:, 3], edge_indices[:, 1]) # indices: start_g2, start_g1, end_g2, end_g1
            k[edge_indices] = edge_aff[b, :ne1[b], :ne2[b]].reshape(-1)
        k = k.reshape(n2max * n1max, n2max * n1max)
        # node-wise affinity
        if node_aff is not None:
            k_diag = torch.diagonal(k)
            k_diag[:] = node_aff[b].transpose(0, 1).reshape(-1)
        ks.append(k)

    return torch.stack(ks, dim=0)


def _check_data_type(input: Tensor):
    """
    Pytorch implementation of _check_data_type
    """
    if type(input) is not Tensor:
        raise ValueError(f'Expected Pytorch Tensor, but got {type(input)}. Perhaps the wrong backend?')


def _check_shape(input, dim_num):
    """
    Pytorch implementation of _check_shape
    """
    return len(input.shape) == dim_num


def _get_shape(input):
    """
    Pytorch implementation of _get_shape
    """
    return input.shape


def _squeeze(input, dim):
    """
    Pytorch implementation of _squeeze
    """
    return input.squeeze(dim)


def _unsqueeze(input, dim):
    """
    Pytorch implementation of _unsqueeze
    """
    return input.unsqueeze(dim)


def _transpose(input, dim1, dim2):
    """
    Pytorch implementaiton of _transpose
    """
    return input.transpose(dim1, dim2)


def _mm(input1, input2):
    """
    Pytorch implementation of _mm
    """
    return torch.mm(input1, input2)
