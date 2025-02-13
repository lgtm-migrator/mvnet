import runtime_path  # isort:skip

import numpy as np

from env import *
from core.tensor import Tensor
from utils.helper import kernelstat
from core.backend.base import ElemwiseOps, ReduceOps, ProcessingOps, ViewOps, CreationOps

np.random.seed(0)

def check_tensor(a, b, atol=0, rtol=1e-4):
    assert a.shape == b.shape
    assert np.allclose(a.numpy(), b, atol=atol, rtol=rtol, equal_nan=True)

def test_lazy_elemwise():
    npa = np.array([[1, 2, 3]]).astype(np.float32)
    a = Tensor(npa, name="a").to("gpu")
    check_tensor(-a, -npa)
    check_tensor(a.log(), np.log(npa))
    check_tensor(a.exp(), np.exp(npa))
    check_tensor(a.relu(), npa*(npa>0))
    check_tensor((a>0), (npa>0).astype(np.float32))

    np_w = np.array([[1, 2, 3]]).astype(np.float32)
    np_x = np.array([[3, 4, 5]]).astype(np.float32)
    np_b = np.array([[0, 1, 0]]).astype(np.float32)
    np_m = np.array([[3, 4, 5]]).astype(np.float32)
    np_n = np.array([[0, 1, 0]]).astype(np.float32)
    w = Tensor(np_w, name="w").to("gpu")
    x = Tensor(np_x, name="x").to("gpu")
    b = Tensor(np_b, name="b").to("gpu")
    m = Tensor(np_m, name="m").to("gpu")
    n = Tensor(np_n, name="n").to("gpu")

    y = w * x + b
    gt = np_w * np_x + np_b
    assert np.allclose(y.numpy(), gt)

    y = b * (w + x)
    gt = np_b * (np_w + np_x)
    assert np.allclose(y.numpy(), gt)

    y = b * (w + x) + (w + b) * x
    gt = np_b * (np_w + np_x) + (np_w + np_b) * np_x
    assert np.allclose(y.numpy(), gt)

    y = b * (w + x) + (m + n)
    gt = np_b * (np_w + np_x) + (np_m + np_n)
    assert np.allclose(y.numpy(), gt)

    y = b * (w + x) + b * (w - x)
    gt = np_b * (np_w + np_x) + np_b * (np_w - np_x)
    assert np.allclose(y.numpy(), gt)

    np_a = np.array([1]).astype(np.float32)
    np_b = np.array([3]).astype(np.float32)
    np_c = np.array([[3, 4, 5]]).astype(np.float32)
    a = Tensor(np_a, name="a").to("gpu")
    b = Tensor(np_b, name="b").to("gpu")
    c = Tensor(np_c, name="c").to("gpu")
    y = -((a + b) * c)
    gt = -((np_a + np_b) * np_c)
    assert np.allclose(y.numpy(), gt)

def _test_lazy_forward():
    kernelstat.reset()
    BS = 64
    idim = 2569
    odim = 10
    x_np = np.random.normal(10, 1, (BS, idim)).astype(np.float32)
    y_np = np.random.normal(10, 1, (BS, odim)).astype(np.float32)
    w_np = np.random.normal(10, 1, (idim, odim)).astype(np.float32)
    b_np = np.zeros((1, odim)).astype(np.float32)

    device = "gpu"
    x = Tensor(x_np, name="x").to(device)
    y = Tensor(y_np, name="y").to(device)
    w = Tensor(w_np, requires_grad=True, name="w").to(device)
    b = Tensor(b_np, requires_grad=True, name="b").to(device)
    w.zero_grad(); b.zero_grad()

    kernelstat.reset()
    pred_tmp = x @ w + b
    pred = pred_tmp / pred_tmp.sum()
    loss = ((pred - y)**2).log().exp().sum()

    pred_tmp_np = x_np @ w_np + b_np
    pred_np = pred_tmp_np / pred_tmp_np.sum()
    loss_np = np.sum(np.exp(np.log((pred_np - y_np)** 2)))

    print(kernelstat.info)
    if LAZY:
        assert kernelstat.total() == 0  # not invoke yet, it' lazy

    kernelstat.reset()
    check_tensor(pred_tmp, pred_tmp_np, rtol=1e-3)
    print(kernelstat.info)
    if LAZY:
        assert kernelstat.get(ProcessingOps)["MATMUL"] == 1
        assert kernelstat.get(ElemwiseOps)["ADD"] == 1

    kernelstat.reset()
    check_tensor(pred, pred_np, rtol=1e-3)
    print(kernelstat.info)
    if LAZY:
        # matmul has been invoked before
        assert kernelstat.get(ProcessingOps)["MATMUL"] == 0
        assert kernelstat.get(ElemwiseOps)["DIV"] == 1

    kernelstat.reset()
    check_tensor(loss, loss_np, rtol=1e-3)
    print(kernelstat.info)
    if LAZY:
        assert kernelstat.get(ProcessingOps)["MATMUL"] == 0
        if not OPT_ELEMWISE_FUSION:
            assert sum(kernelstat.get(ElemwiseOps).values()) == 4 + 3
        else:
            assert sum(kernelstat.get(ElemwiseOps).values()) == 1 + 3

    kernelstat.reset()
    check_tensor(loss, loss_np, rtol=1e-3)
    print(kernelstat.info)
    if LAZY:
        assert kernelstat.get(ElemwiseOps)["NOOP"] == 1
        assert kernelstat.total() == 1

def test_lazy_backward():
    BS = 64
    idim = 2569
    odim = 10
    x_np = np.random.normal(0, 1, (BS, idim)).astype(np.float32)
    y_np = np.random.normal(0, 1, (BS, odim)).astype(np.float32)
    w_np = np.random.normal(0, 1, (idim, odim)).astype(np.float32)
    b_np = np.zeros((1, odim)).astype(np.float32)

    device = "gpu"
    x = Tensor(x_np, name="x").to(device)
    y = Tensor(y_np, name="y").to(device)
    w = Tensor(w_np, requires_grad=True, name="w").to(device)
    b = Tensor(b_np, requires_grad=True, name="b").to(device)
    w.zero_grad(); b.zero_grad()

    pred = (x @ w + b).relu()
    loss = ((pred - y)**2).sum()
    loss.backward()

    pred_np_ = (x_np @ w_np + b_np)
    pred_np = pred_np_ * (pred_np_ > 0)
    loss_np = ((pred_np - y_np) ** 2).sum()

    #w_grad = w.grad.numpy()
    #w_grad_np = (x_np.T @ (2 * (pred_np - y_np) * (pred_np_ > 0)))
    #assert np.allclose(w_grad, w_grad_np, atol=1e-2)
    #check_tensor(loss, loss_np)
    check_tensor(pred, pred_np, atol=1e-3)

def test_graph_optimizer():
    BS = 64
    idim = 2569
    odim = 10
    x_np = np.random.normal(0, 1, (BS, idim)).astype(np.float32)
    y_np = np.random.normal(0, 1, (BS, odim)).astype(np.float32)
    w_np = np.random.normal(0, 1, (idim, odim)).astype(np.float32)
    b_np = np.zeros((1, odim)).astype(np.float32)

    device = "gpu"
    x = Tensor(x_np, name="x").to(device)
    y = Tensor(y_np, name="y").to(device)
    w = Tensor(w_np, requires_grad=True, name="w").to(device)
    b = Tensor(b_np, requires_grad=True, name="b").to(device)
    w.zero_grad(); b.zero_grad()

    pred_tmp = x @ w + b
    pred = pred_tmp / pred_tmp.sum()
    cost = (pred - y) ** 2
    loss = cost.log().exp().sum()
    loss = cost.sum()

    pred_tmp_np = x_np @ w_np + b_np
    pred_np = pred_tmp_np / pred_tmp_np.sum()
    loss_np = np.sum(np.exp(np.log((pred_np - y_np)** 2)))
    loss_np = np.sum((pred_np - y_np)**2)
    check_tensor(loss, loss_np)

def test_minimal():
    if not LAZY: return
    from utils.helper import kernelstat
    np.random.seed(0)
    n_epoch = 50
    lr = 0.0001

    BS = 2**6
    idim = 2**8
    odim = 2**6
    x_np = np.random.normal(0, 1, (BS, idim)).astype(np.float32)  # (64, 256)
    y_np = np.random.normal(0, 1, (BS, odim)).astype(np.float32)  # (64, 64)
    w_np = np.random.normal(0, 1, (idim, odim)).astype(np.float32)  # (256, 64)
    b_np = np.zeros((1, odim)).astype(np.float32)  # (1, 64)

    x, y, w, b = x_np.copy(), y_np.copy(), w_np.copy(), b_np.copy()
    for epoch in range(n_epoch):
        pred = x @ w + b
        err = pred - y
        loss = (err**2).sum()
        dw = x.T @ (2 * err)
        db = (2 * err).sum(axis=0, keepdims=True)
        w -= lr * dw
        b -= lr * db
    loss_final, w_final, b_final = loss, w, b

    import time
    st = time.monotonic()
    devices = ("gpu",)
    for device in devices:
        x = Tensor(x_np).to(device)
        y = Tensor(y_np).to(device)
        w = Tensor(w_np, requires_grad=True).to(device)
        b = Tensor(b_np, requires_grad=True).to(device)
        for epoch in range(n_epoch):
            w.zero_grad()
            b.zero_grad()
            pred = x @ w + b
            err = pred - y
            loss = (err ** 2).sum()
            #print("!!!!!", loss.array.numpy())
            #print(kernelstat.info)
            #print(kernelstat.total())
            #return
            loss.backward()
            w -= lr * w.grad
            b -= lr * b.grad
            if LAZY and device == "gpu":
                w.array = w.array.eager()
                #print(kernelstat.info)
                #print(kernelstat.total())
                #return
                b.array = b.array.eager()
        assert np.allclose(loss.numpy(), loss_final, rtol=1e-3)
        assert np.allclose(w.numpy(), w_final, rtol=1e-3)
        assert np.allclose(b.numpy(), b_final, rtol=1e-3)
    #print("!!!!!!!!", time.monotonic() - st)
    #print(kernelstat.info)
    #print(kernelstat.total())

def test_graph_optimizer_constant_folding_badcases():
    if not LAZY: return

    a = Tensor(2).to("gpu")
    b = Tensor(2).to("gpu")
    c = a + b
    assert(c.numpy() == 4)

    a_np = np.log(np.tile(np.expand_dims(np.array(2), [0,1,2]), (3, 4, 5)))
    a = Tensor(2).to("gpu")
    a = a.reshape((1, 1, 1)).expand((3, 4, 5)).log()
    assert np.allclose(a.numpy(), a_np, rtol=1e-3)

    a_np = np.log(np.tile(np.expand_dims(np.array(2), [0,1,2]), (3, 4, 5))) + 1
    a = Tensor(2).to("gpu")
    a = a.reshape((1, 1, 1)).expand((3, 4, 5)).log() + 1
    assert np.allclose(a.numpy(), a_np, rtol=1e-3)

    a_np = np.log(np.tile(np.expand_dims(np.array(2), [0,1,2]), (3, 4, 5))) ** 2
    a = Tensor(2).to("gpu")
    a = a.reshape((1, 1, 1)).expand((3, 4, 5)).log()
    a = a ** 2
    assert np.allclose(a.numpy(), a_np, rtol=1e-3)

    a = Tensor(2).to("gpu")
    b = a.sum()
    assert np.allclose(b.numpy(), 2, rtol=1e-3)

    a_np = np.random.normal(0, 1, (10, 10)).astype(np.float32)
    b_np = np.exp(a_np.sum())
    a = Tensor(a_np).to("gpu")
    b = a.sum().exp()
    assert np.allclose(b.numpy(), b_np, rtol=1e-3)


def test_graph_optimizer_elemwise_fusion_badcases():
    if not LAZY: return

    # should fuse
    a_np = np.random.normal(0, 1, (10, 10))
    tmp = np.exp(a_np)
    b_np = np.exp(tmp) + np.log(tmp)
    a = Tensor(a_np).to("gpu")
    a = a.exp()
    b = a.exp() + a.log()
    assert np.allclose(b.numpy(), b_np, rtol=1e-3)

    # should NOT fuse
    a_np = np.random.normal(0, 1, (10, 10))
    tmp = np.exp(a_np)
    b_np = np.exp(tmp) + np.log(tmp).sum()
    a = Tensor(a_np).to("gpu")
    a = a.exp()
    b = a.exp() + a.log().sum()
    assert np.allclose(b.numpy(), b_np, rtol=1e-3)

    # should NOT fuse
    a_np = np.random.normal(0, 1, (10, 10))
    b_np = np.random.normal(0, 1, (10, 10))
    c_np = np.exp(a_np + b_np)
    d_np = c_np / c_np.sum()
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = (a + b).exp()
    d = c / c.sum()
    assert np.allclose(d.numpy(), d_np, rtol=1e-3)

def test_graph_optimizer_viewop_pruning_badcases():
    if not LAZY: return

    a_np = np.random.normal(0, 1, (1, 4))
    b_np = np.tile(np.exp(a_np), (4, 1))
    b_np = b_np * (b_np > 0)
    a = Tensor(a_np).to("gpu")
    b = a.exp().expand((4, 4))
    b = b.relu()
    assert np.allclose(b.numpy(), b_np, rtol=1e-3)

    a_np = np.random.normal(0, 1, (2, 2))
    b_np = np.tile((np.exp(a_np) - 1).reshape((1, 4)), (4, 1))
    b_np = b_np * (b_np > 0)
    a = Tensor(a_np).to("gpu")
    b = (a.exp() - 1).reshape((1, 4)).expand((4, 4))
    b = b.relu()
    assert np.allclose(b.numpy(), b_np, rtol=1e-3)

    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.tile(a_np.reshape((1, 25)), (25, 1))
    b_np = b_np * (b_np > 0)
    a = Tensor(a_np).to("gpu")
    b = a.reshape((1, 25)).expand((25, 25)).relu()
    assert np.allclose(b.numpy(), b_np, rtol=1e-3)

    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu").reshape((1, 25)).expand((25, 25))
    b = Tensor(b_np).to("gpu").reshape((1, 25)).expand((25, 25))
    a_np = np.tile(a_np.reshape((1, 25)), (25, 1))
    b_np = np.tile(b_np.reshape((1, 25)), (25, 1))
    c_np = a_np + b_np
    c = a + b
    assert np.allclose(c.numpy(), c_np, rtol=1e-3)

    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu").reshape((1, 25)).expand((25, 25))
    b = Tensor(b_np).to("gpu").reshape((1, 25)).expand((25, 25))
    a_np = np.tile(a_np.reshape((1, 25)), (25, 1))
    b_np = np.tile(b_np.reshape((1, 25)), (25, 1))
    c_np = np.exp(a_np) + b_np
    c = a.exp() + b
    assert np.allclose(c.numpy(), c_np, rtol=1e-3)

def test_graph_optimizer_elemwise_processing_fusion():
    if not LAZY: return

    # input: multiple processing ops, do not fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = a @ b + a @ b
    assert np.allclose(c.numpy(), a_np @ b_np + a_np @ b_np, rtol=1e-3)

    # input: processing & reduce, do not fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = a @ b + a.sum()
    assert np.allclose(c.numpy(), a_np @ b_np + a_np.sum(), rtol=1e-3)

    # input: processing and constant, fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = a @ b + 1
    assert np.allclose(c.numpy(), a_np @ b_np + 1, rtol=1e-3)

    # input: processing and elemwise, fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = a @ b + a
    assert np.allclose(c.numpy(), a_np @ b_np + a_np, rtol=1e-3)

    # input: processing and elemwise, fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    c_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = Tensor(c_np).to("gpu")
    d = (a @ b + a) @ c + b
    assert np.allclose(d.numpy(), (a_np @ b_np + a_np) @ c_np + b_np, rtol=1e-3)

    # input: only one processing op, fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    c_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = Tensor(c_np).to("gpu")
    d = ((a @ b).exp() @ c).exp()
    assert np.allclose(d.numpy(), np.exp(np.exp(a_np @ b_np) @ c_np), rtol=1e-3)

    # dep_node outdegree > 1, do not fuse
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = a.sum() * ((a @ b) + 1)
    assert np.allclose(c.numpy(), a_np.sum() * (a_np @ b_np + 1), rtol=1e-3)

    # dep_node is lazy
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    c_np = np.random.normal(0, 1, (5, 5))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = Tensor(c_np).to("gpu")
    a = a.exp()
    a = a + a.exp()
    d = b @ b + a
    assert np.allclose(d.numpy(), b_np @ b_np + (np.exp(a_np) + np.exp(np.exp(a_np))), rtol=1e-3)

    # depnode is non-contiguous
    a_np = np.random.normal(0, 1, (5, 5))
    b_np = np.random.normal(0, 1, (5, 5))
    c_np = np.random.normal(0, 1, (1, 1))
    a = Tensor(a_np).to("gpu")
    b = Tensor(b_np).to("gpu")
    c = Tensor(c_np).to("gpu")
    c = c.expand((5, 5))

    c_np = np.tile(c_np, (5, 5))
    d = a @ b + c.exp()
    #aa = d.numpy()
    #bb = a_np @ b_np + np.exp(c_np)
    assert np.allclose(d.numpy(), a_np @ b_np + np.exp(c_np), rtol=1e-3)
