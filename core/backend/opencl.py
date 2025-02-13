import copy
from types import SimpleNamespace
from functools import lru_cache

import numpy as np
import pyopencl
import pyopencl.clrandom

from env import *
from core.backend.base import Array, ElemwiseOps, ProcessingOps, ReduceOps, ViewOps, CreationOps
from core.dtype import int32, float32
from core.jit.graph import GraphOptimizer
from utils.math import prod
from utils.helper import kernelstat

ELEMWISE_MAPPING = {
    ElemwiseOps.NOOP: "A", ElemwiseOps.NEG: "-A", ElemwiseOps.EXP: "exp(A)", ElemwiseOps.LOG: "log(A)",
    ElemwiseOps.ADD: "A+B", ElemwiseOps.SUB: "A-B", ElemwiseOps.DIV: "A/B", ElemwiseOps.MUL: "A*B", ElemwiseOps.POW: "pow(A,B)",
    ElemwiseOps.EQ: "(float)isequal(A,B)", ElemwiseOps.GE: "(float)isgreaterequal(A,B)", ElemwiseOps.GT: "(float)isgreater(A,B)",
    ElemwiseOps.RELU: "max(A,0.0f)", ElemwiseOps.DRELU: "B>0?A:0.0f"
}
REDUCE_AGG_FN = {ReduceOps.SUM: "A+B", ReduceOps.MAX: "max(A,B)"}
REDUCE_PAD_VAL = {ReduceOps.SUM: "0.0f", ReduceOps.MAX: "-INFINITY"}


class CLContext:
    def __init__(self):
        self.ctx, self.queue = None, None
        platform = pyopencl.get_platforms()[0]
        devices = platform.get_devices(device_type=pyopencl.device_type.GPU)
        if len(devices) == 0:
            devices = platform.get_devices(device_type=pyopencl.device_type.CPU)
        self.ctx = pyopencl.Context(devices)
        self.queue = pyopencl.CommandQueue(self.ctx)
        self.rng = pyopencl.clrandom.PhiloxGenerator(self.ctx, seed=0)
        self.info = {"build_cnt": 0}

        alloc = pyopencl.tools.ImmediateAllocator(self.queue)
        self.mem_pool = pyopencl.tools.MemoryPool(alloc)

    @lru_cache(maxsize=None)
    def build(self, name, program):
        self.info["build_cnt"] += 1
        if DEBUG: print(f"[DEBUG] program {name}: \n {program}")
        kernel = pyopencl.Program(self.ctx, program).build().__getattr__(name)
        return lambda *args: kernel(self.queue, *args)

    def alloc_local(self, size):
        return pyopencl.LocalMemory(size)

    def alloc_buffer(self, shape, dtype, hostbuf=None):
        size = int(dtype().itemsize * prod(shape))
        buffer = self.mem_pool.allocate(size)
        if hostbuf is not None:
            assert isinstance(hostbuf, np.ndarray) and hostbuf.dtype == dtype
            self.enqueue("copy", buffer, hostbuf)
        return buffer

    def enqueue(self, task, *args, **kwargs):
        getattr(pyopencl, f"enqueue_{task}")(self.queue, *args, **kwargs)

cl = CLContext()

def elemwise_op(op_info):
    inp = {k: v for k, v in op_info.operands.items() if v.constant_value is None}
    const_inp = {k: v for k, v in op_info.operands.items() if v.constant_value is not None}
    shape, dtype = op_info.args["shape"], op_info.args["dtype"]
    ret = op_info.args["out"] if op_info.args.get("out", None) is not None else CLArray(shape=shape, dtype=dtype)
    op = cl.build("ElemwiseOp", f"""
    __kernel void ElemwiseOp(
      // strides
      {''.join(''.join(f'int {n}_s{i}, ' for i in range(arr.ndim)) for n, arr in inp.items())}
      {''.join(f'int res_s{i}, ' for i in range(ret.ndim))}
      // offset
      {''.join(f'int {n}_ofst, ' for n in inp)}
      // buffer inputs
      {''.join(f'__global const float *inp_{n}, ' for n in inp)}
      // constant inputs
      {''.join(f'const float {n}, ' for n in const_inp)}
      __global float *ret
    ) {{
      {''.join(f'int {n}_i=0; ' for n in inp)}
      int idx=0, gl_id=get_global_id(0); int ptr=gl_id;
      // calculate element indices
      {''.join(f'idx=ptr/res_s{i}; ptr%=res_s{i}; ' + ''.join(f'{n}_i+=idx*{n}_s{i}; ' for n in inp if i < inp[n].ndim) for i in range(ret.ndim))}
      // get elements from input
      {''.join(f'float {n}=inp_{n}[{n}_i+{n}_ofst]; ' for n in inp)}
      ret[gl_id] = {op_info.code};
    }}
    """)
    args = [int32(s) for x in list(inp.values()) + [ret] for s in x.strides]
    args += [int32(x.offset) for x in inp.values()]
    args += [x.buffer for x in inp.values()]
    args += [float32(x.constant_value) for x in const_inp.values()]
    e = op((prod(shape),), None, *args, ret.buffer)
    kernelstat.log(op_info.operator)
    return ret

def matmul_op(op_info):
    # rule: https://numpy.org/doc/stable/reference/generated/numpy.matmul.html
    a, b = op_info.operands.values()
    ret_shape = op_info.ret_shape
    if op_info.args.get("out", None):
        ret = op_info.args["out"]
        assert ret.c_contiguous and ret.shape == ret_shape
    else:
        ret = CLArray(shape=ret_shape, dtype=a.dtype)
    BS, M, K, N = prod(a.shape[:-2]), a.shape[-2], a.shape[-1], b.shape[-1]
    gs = 1
    while gs <= 8 and M % gs == 0 and N % gs == 0 and K % gs == 0 and gs <= K and gs <= M and gs <= N:
        gs *= 2
    gs //= 2
    if DEBUG: print(f"[DEBUG] BS:{BS} M:{M} K:{K} N:{N} grp_size:{gs}")

    # extra post compute
    # TODO: refactor extra
    extra_inp, extra_const_inp = {}, {}
    for k, v in op_info.args.get("extra", {}).get("operands", {}).items():
        if v.constant_value is None: extra_inp[k] = v
        else: extra_const_inp[k] = v
    extra_code = op_info.args.get("extra", {}).get("code", "acc")
    extra_gl2lc, extra_strides = "", ""
    if extra_inp:
        extra_strides = ''.join(''.join(f'int {n}_s{i}, ' for i in range(arr.ndim)) for n, arr in extra_inp.items()) + ''.join(f'int res_s{i}, ' for i in range(ret.ndim))
        extra_gl2lc = ''.join(f'idx=ptr/res_s{i}; ptr%=res_s{i}; ' + ''.join(f'{n}_i+=idx*{n}_s{i}; ' for n, arr in extra_inp.items() if i < arr.ndim) for i in range(ret.ndim))

    op = cl.build("matmul_op", f"""
    __kernel void matmul_op(
      int BS, int M, int N, int K,
      {''.join(f'int A_s{i}, int B_s{i}, ' for i in range(3))}
      int a_ofst, int b_ofst,
      {extra_strides}
      {''.join(f'__global const float *inp_{n}, ' for n in extra_inp)}
      {''.join(f'const float {n}, ' for n in extra_const_inp)}
      __global const float *A, __global const float *B, __global float *C
    ) {{
      int bs=get_global_id(0), m=get_global_id(1), n=get_global_id(2), i=get_local_id(1), j=get_local_id(2);
      __local float Alcl[{gs}][{gs}], Blcl[{gs}][{gs}];
      float acc = 0.0f;
      for (int t=0; t<K/{gs}; t++) {{
        Alcl[i][j] = A[bs*A_s0 + m*A_s1 + (t*{gs}+j)*A_s2 + a_ofst];
        Blcl[i][j] = B[bs*B_s0 + (t*{gs}+i)*B_s1 + n*B_s2 + b_ofst];
        barrier(CLK_LOCAL_MEM_FENCE);
        for (int k=0; k<{gs}; k++) acc += Alcl[i][k] * Blcl[k][j];
        barrier(CLK_LOCAL_MEM_FENCE);
      }}
      // C[bs*M*N+m*N+n] = acc;
      // NOTE: handle non-contiguous extra_inp
      int k = bs*M*N+m*N+n, ptr=k, idx=0;
      {''.join(f'int {n}_i=0; ' for n in extra_inp)}
      {extra_gl2lc}
      {''.join(f'float {n}=inp_{n}[{n}_i]; ' for n in extra_inp)}
      C[k] = {extra_code};
    }}""")
    strides = [s for ss in zip(a.strides, b.strides) for s in ss]
    args = [int32(x) for x in [BS, M, N, K] + strides + [a.offset, b.offset]]
    if extra_inp:
        args += [int32(s) for x in list(extra_inp.values()) + [ret] for s in x.strides]
    args += [x.buffer for x in extra_inp.values()]
    args += [float32(x.constant_value) for x in extra_const_inp.values()]
    e = op((BS, M, N), (1, gs, gs), *args, a.buffer, b.buffer, ret.buffer)
    kernelstat.log(op_info.operator)
    return ret

def reduce_op(op_info):
    x = next(iter(op_info.operands.values()))
    x_shp = x.shape
    axis, keepdims = op_info.args["axis"], op_info.args["keepdims"]
    if axis is None: axis, x_shp = 0, (prod(x.shape),)
    size = x_shp[axis]

    grp_size = 2
    max_work_group_size = cl.queue.device.max_work_group_size
    while grp_size != max_work_group_size and grp_size < size:
        grp_size *= 2

    def calculate_ret_shape(x_shp, axis, keepdims, grp_size, n_grps):
        if n_grps <= 1:
            ret_shape = [d for i, d in enumerate(x_shp) if i != axis]
            if keepdims: ret_shape.insert(axis, 1)
            return tuple(ret_shape)
        return tuple(n_grps if i == axis else d for i, d in enumerate(x_shp))

    n_grps = (size + grp_size - 1) // grp_size
    ret_shape = calculate_ret_shape(x_shp, axis, keepdims, grp_size, n_grps)
    # NOTE: for array with constant_value, return a new array filled with the value directly
    if x.constant_value is not None:
        return CLArray.full(ret_shape, x.constant_value, x.dtype)
    ret = CLArray(shape=ret_shape, dtype=x.dtype)

    # merge non-target axes
    p1 = [prod(x_shp[:axis])] if axis!=0 else []
    p2 = [prod(x_shp[axis+1:])] if axis!=len(x_shp)-1 else []
    global_size = (*p1, grp_size*n_grps, *p2)
    axis, ndim = len(p1), len(global_size)

    a = [f"gl_id_{i}" for i in range(ndim)]
    b = [f"gl_s_{i}" for i in range(ndim)]
    c = ["*".join(b[i+1:]) for i in range(ndim-1)] + ["1"]
    gl2lcl = "+".join([f"{a_}*{c_}" for a_, c_ in zip(a, c)])
    a = [(f"grp_id_{i}" if i == axis else f"gl_id_{i}") for i in range(ndim)]
    b = [f"(gl_s_{i}/grp_s_{i})" for i in range(ndim)]
    c = ["*".join(b[i+1:]) for i in range(ndim-1)] + ["1"]
    lcl2gl = "+".join([f"{a_}*{c_}" for a_, c_ in zip(a, c)])
    # NOTE: calculate offset to get the proper global index
    offset = f"gl_id_0*{'0' if axis==0 else '1' if axis==ndim-1 else 'gl_s_2'}*(gl_s_{axis}-size)"
    op = cl.build("reduce_op", f"""
    __kernel void reduce_op(
      int size, int ofst, __global const float *inp, __local float *lcl, __global float *ret
    ) {{
      {''.join([f'int gl_id_{i}=get_global_id({i});int gl_s_{i}=get_global_size({i});int grp_id_{i}=get_group_id({i});int grp_s_{i}=get_local_size({i});' for i in range(ndim)])}
      int lcl_id = get_local_id({axis});
      lcl[lcl_id] = gl_id_{axis} < size ? inp[{gl2lcl}-{offset}+ofst] : {REDUCE_PAD_VAL[op_info.operator]};
      barrier(CLK_LOCAL_MEM_FENCE);
      for (int stride = grp_s_{axis}>>1; stride > 0; stride>>=1) {{
        float A = lcl[lcl_id], B = lcl[lcl_id+stride];
        if (lcl_id<stride) lcl[lcl_id] = {REDUCE_AGG_FN[op_info.operator]};
        barrier(CLK_LOCAL_MEM_FENCE);
      }}
      if (lcl_id == 0) ret[{lcl2gl}] = lcl[0];
    }}
    """)
    local_mem = cl.alloc_local(x.dtype().itemsize * grp_size)
    local_size = tuple(grp_size if i == axis else 1 for i in range(ndim))
    e = op(global_size, local_size, int32(size), int32(x.offset), x.buffer, local_mem, ret.buffer)
    if DEBUG: print(f"[DEBUG] x_shp: {x_shp} ret_shape: {ret_shape} grp_size: {grp_size} n_grps: {n_grps} size: {size} global_size: {global_size} local_size: {local_size} axis={axis} ndim={ndim} offset={offset}")
    kernelstat.log(op_info.operator)
    if n_grps > 1:
        op_info = SimpleNamespace(operator=op_info.operator, operands={"A": ret}, args=op_info.args)
        ret = reduce_op(op_info)
    return ret

def view_op(op_info):
    x = next(iter(op_info.operands.values()))
    inst = copy.copy(x)
    if op_info.operator == ViewOps.EXPAND:
        shape = op_info.args["shape"]
        strides = [0 if s1<s2 else inst.strides[i] for i, (s1,s2) in enumerate(zip(inst.shape, shape))]
    elif op_info.operator == ViewOps.RESHAPE:
        shape = op_info.args["shape"]
        strides = (prod(shape[i+1:]) if inst.c_contiguous else prod(shape[:i]) for i in range(len(shape)))
    elif op_info.operator == ViewOps.PERMUTE:
        axes = op_info.args["axes"]
        shape = tuple(inst.shape[a] for a in axes)
        strides = tuple(inst.strides[a] for a in axes)
    inst.shape, inst.strides = tuple(shape), tuple(strides)
    inst.c_contiguous, inst.f_contiguous = inst._calculate_contiguity()
    return inst

def register_elemwise_op(func):
    def wrapper(*inputs, **kwargs):
        if len(inputs) > 1 and len(set([i.shape for i in inputs])) > 1:
            inputs = Array.broadcast(*inputs)
        op = func(*inputs)
        code = ELEMWISE_MAPPING[op]
        kwargs = {**kwargs, "shape": inputs[0].shape, "dtype": inputs[0].dtype}
        op_info = SimpleNamespace(operator=op, code=code, operands=dict(zip("AB", inputs)), args=kwargs)
        if not LAZY or (kwargs.get("eager", False) and op == ElemwiseOps.NOOP):
            return invoke(op_info)
        return CLArray(shape=inputs[0].shape, dtype=inputs[0].dtype, op_info=op_info, is_lazy=True)
    return wrapper

def register_reduce_op(func):
    def wrapper(x, axis=None, keepdims=False):
        op = func(x, axis=axis, keepdims=keepdims)
        x = x.contiguous() if not x.c_contiguous else x
        op_info = SimpleNamespace(operator=op, operands={"A": x}, args=dict(axis=axis, keepdims=keepdims))
        if not LAZY: return invoke(op_info)
        ret_shape = () if axis is None else [d for i, d in enumerate(x.shape) if i != axis]
        if keepdims: ret_shape.insert(axis, 1)
        return CLArray(shape=tuple(ret_shape), dtype=x.dtype, op_info=op_info, is_lazy=True)
    return wrapper

def invoke(op_info):
    optype = type(op_info.operator)
    if optype is ElemwiseOps:
        return elemwise_op(op_info)
    elif optype is ReduceOps:
        return reduce_op(op_info)
    elif optype is ProcessingOps:
        return matmul_op(op_info)
    elif optype is ViewOps:
        return next(iter(op_info.operands.values()))
    else:
        raise ValueError(f"Invoke invalid operator {op_info.operator}")

class CLArray(Array):
    def __init__(self, data=None, shape=None, dtype=float32, op_info=None, is_lazy=False):
        super().__init__(shape, dtype, op_info, is_lazy)
        # TODO: replace simplenamespace
        self.op_info = SimpleNamespace(operator=None, operands={}, args={}) if op_info is None else op_info
        if not self.is_lazy:
            if isinstance(data, pyopencl.Buffer):
                self.buffer = data
                assert self.shape is not None, "Can not infer shape when initialize using clbuffer"
            else:
                if data is not None:
                    data = np.asarray(data, dtype=self.dtype)
                    self.shape = data.shape
                    if prod(self.shape) == 1:
                        self.constant_value = float(data)

                assert self.shape is not None, "Array shape is None!"
                if self.constant_value is None:
                    # NOTE: do not allocate buffer for array with a single element
                    self.buffer = cl.alloc_buffer(self.shape, self.dtype, data)
        # meta infos (https://numpy.org/doc/stable/dev/internals.html#numpy-internals)
        self.strides = tuple(prod(self.shape[i+1:]) for i in range(self.ndim))
        self.c_contiguous, self.f_contiguous = self._calculate_contiguity()
        self.offset = 0  # offset relative to the beginning of the buffer

    def to_constant(self, value):
        self.is_lazy = False
        self.op_info = SimpleNamespace(operator=None, operands={}, args={})
        self.constant_value = value

    @property
    def size(self):
        return self.buffer.size

    def numpy(self):
        arr = self.eager() if self.is_lazy else self
        data = np.empty(arr.shape, dtype=arr.dtype)
        cl.enqueue("copy", data, arr.contiguous(eager=True).buffer, is_blocking=True)
        return data

    # ##### Elemwise Ops #####
    for op in ("neg", "exp", "log", "relu"):
        exec(f"@register_elemwise_op\ndef {op}(self, out=None): return ElemwiseOps.{op.upper()}")
    for op in ("add", "sub", "div", "mul", "pow", "eq", "ge", "gt", "contiguous", "drelu"):
        exec(f"@register_elemwise_op\ndef {op}(self, other, out=None): return ElemwiseOps.{op.upper()}")
    exec(f"@register_elemwise_op\ndef contiguous(self): return ElemwiseOps.NOOP")

    # ##### Reduce Ops #####
    for op in ("sum", "max"):
        exec(f"@register_reduce_op\ndef {op}(self, axis=None, keepdims=False): return ReduceOps.{op.upper()}")

    # ##### Processing Ops #####
    def matmul(self, other, out=None):
        a, b = self, other
        squeezes = []
        if a.ndim == 1: a = a.reshape((1, *a.shape)); squeezes.append(0)
        if b.ndim == 1: b = b.reshape((*b.shape, 1)); squeezes.append(-1)
        ret_shape = tuple((*a.shape[:-1], b.shape[-1]))

        if a.ndim > 3: a = a.reshape((prod(a.shape[:-2]), *a.shape[2:]))
        if b.ndim > 3: b = b.reshape((prod(b.shape[:-2]), *b.shape[2:]))
        if a.ndim == 2: a = a.reshape((1, *a.shape))
        if b.ndim == 2: b = b.reshape((1, *b.shape))
        if a.shape[0] != b.shape[0]:
            assert a.shape[0] == 1 or b.shape[0] == 1
            if a.shape[0] == 1 and b.shape[0] != 1: a = a.expand((b.shape[0], *a.shape[1:]))
            if b.shape[0] == 1 and a.shape[0] != 1: b = b.expand((a.shape[0], *b.shape[1:]))
        assert a.shape[0] == b.shape[0] and a.shape[2] == b.shape[1], \
                f"invalid shape for matmul {a.shape} @ {b.shape}"
        operands = {"A": a, "B": b}
        args = {"out": out}
        op_info = SimpleNamespace(operator=ProcessingOps.MATMUL, operands=operands, args=args, ret_shape=ret_shape)
        arr = invoke(op_info) if not LAZY else CLArray(shape=ret_shape, dtype=a.dtype, op_info=op_info, is_lazy=True)
        for axis in squeezes:
            arr = arr.squeeze(axis)
        return arr

    # ##### View Ops #####
    def reshape(self, shape):
        if -1 in shape:
            size = prod(self.shape)
            assert shape.count(-1) <= 1, "Only one dimension can be inferred"
            axis = shape.index(-1)
            infer = prod([s for s in shape if s != -1])
            assert size % infer == 0, f"Shape {shape} invalid for size {size}"
            shape = (*shape[:axis], size // infer, *shape[axis+1:])
        shape = tuple(shape)
        assert prod(shape) == prod(self.shape), f"Can not reshape {self.shape} to {shape}"
        if not self.c_contiguous and not self.f_contiguous:
            self = self.contiguous()
        op_info = SimpleNamespace(operator=ViewOps.RESHAPE, operands={"A": self}, args={"shape": shape})
        arr = view_op(op_info)
        if LAZY: arr.op_info = op_info
        return arr

    def expand(self, shape):
        op_info = SimpleNamespace(operator=ViewOps.EXPAND, operands={"A": self}, args={"shape": shape})
        arr = view_op(op_info)
        if LAZY: arr.op_info = op_info
        return arr

    def permute(self, axes):
        assert sorted(list(axes)) == list(range(self.ndim)), f"Invalid axes {axes}"
        op_info = SimpleNamespace(operator=ViewOps.PERMUTE, operands={"A": self}, args={"axes": axes})
        arr = view_op(op_info)
        if LAZY: arr.op_info = op_info
        return arr

    def squeeze(self, axis=None):
        if axis is None:
            axis = [i for i, s in enumerate(self.shape) if s == 1]
        elif isinstance(axis, int):
            axis = [axis]
        axis = tuple([a if a != -1 else self.ndim - 1 for a in axis])
        shape = tuple([s for i, s in enumerate(self.shape) if i not in axis or self.shape[i] != 1])
        if shape == self.shape:
            return self
        return self.reshape(shape)

    def __getitem__(self, key):
        # TODO: handle step
        is_basic = lambda k: isinstance(k, (slice, int))
        assert is_basic(key) or all(is_basic(k) for k in key), f"Advantage indexing not supported yet. {key}"
        key = (key,) if is_basic(key) else key
        inst = copy.copy(self)
        reduce = []
        shape = list(inst.shape)
        for i, k in enumerate(key):
            if isinstance(k, int):  # indexing
                if k < 0: k += inst.shape[i]
                assert 0 <= k < inst.shape[i], f"Invalid indexing {key[i]} for tensor {inst.shape}"
                inst.offset += inst.strides[i] * k
                reduce.append(i)
            if isinstance(k, slice):  # slicing
                start = 0 if k.start is None else k.start
                if start < 0: start += inst.shape[i]
                stop = inst.shape[i] if k.stop is None else k.stop
                if stop < 0: stop += inst.shape[i]
                assert 0 <= start < stop <= inst.shape[i], f"Invalid slicing {key[i]} for tensor {inst.shape}"
                shape[i] = stop - start
                inst.offset += inst.strides[i] * start
                inst.c_contiguous, inst.f_contiguous = inst._calculate_contiguity()
        inst.shape = tuple(s for i, s in enumerate(shape) if i not in reduce)
        inst.strides = tuple(s for i, s in enumerate(inst.strides) if i not in reduce)
        return inst

    def __setitem__(self, key, value):
        item = self[key]
        # unary_op("noop", value, ret=item)
        # TODO: implement assign ops
        assert False

    # ##### Creation Ops #####
    @classmethod
    def empty(cls, shape, dtype=float32):
        return cls(shape=shape, dtype=dtype)

    @classmethod
    def full(cls, shape, value, dtype=float32):
        inst = cls(shape=shape, dtype=dtype)
        cl.enqueue("fill_buffer", inst.buffer, inst.dtype(value), 0, inst.size)
        return inst

    @classmethod
    def uniform(cls, a, b, shape, dtype=float32):
        buffer = cl.rng.uniform(a=a, b=b, shape=shape, dtype=dtype, cq=cl.queue).data
        return cls(data=buffer, shape=shape, dtype=dtype)

    @classmethod
    def normal(cls, loc, scale, shape, dtype=float32):
        buffer = cl.rng.normal(mu=loc, sigma=scale, shape=shape, dtype=dtype, cq=cl.queue).data
        return cls(data=buffer, shape=shape, dtype=dtype)

    # ##### Lazy #####
    def update_from_eager(self, eager):
        self.buffer = eager.buffer
        self.is_lazy = False
        self.op_info = SimpleNamespace(operator=None, operands={})
        self.constant_value = eager.constant_value
        return self

    def eager(self):
        def recursive_eager(node):
            for dep_node in node.op_info.operands.values():
                if dep_node.is_lazy:
                    recursive_eager(dep_node)
            if node.is_lazy:
                eager = invoke(node.op_info)
                node.update_from_eager(eager)

        graphoptimizer = GraphOptimizer(root=self)
        graphoptimizer._rename_operands(self)
        # naive graph
        if GRAPH:
            graph_name = "net"
            print(f"[GRAPH] {graphoptimizer.count(self)} nodes")
            graphoptimizer.visualize(self, graph_name)
        # opt1: view op pruning
        if OPT_VIEWOP_PRUNING:
            graphoptimizer._viewop_pruning(self)
            if GRAPH:
                print(f"[GRAPH] OPT_VIEWOP_PRUNING: #nodes={graphoptimizer.count(self)}")
                graph_name += "_1"
                graphoptimizer.visualize(self, graph_name)
        # opt2: constant folding
        if OPT_CONSTANT_FOLDING:
            graphoptimizer._constant_folding(self)
            if GRAPH:
                print(f"[GRAPH] OPT_CONSTANT_FOLDING: #nodes={graphoptimizer.count(self)}")
                graph_name += "_2"
                graphoptimizer.visualize(self, graph_name)
        # opt3: elemwise fusion
        if OPT_ELEMWISE_FUSION:
            graphoptimizer._elemwise_fusion(self)
            if GRAPH:
                graph_name += "_3"
                print(f"[GRAPH] OPT_ELEMWISE_FUSION: #nodes={graphoptimizer.count(self)}")
                graphoptimizer.visualize(self, graph_name)
        # opt4: elemwise processing fusion
        if OPT_ELEMWISE_PROCESSING_FUSION:
            graphoptimizer._elemwise_processing_fusion(self)
            if GRAPH:
                graph_name += "_4"
                print(f"[GRAPH] OPT_ELEMWISE_PROCESSING_FUSION: #nodes={graphoptimizer.count(self)}")
                graphoptimizer.visualize(self, graph_name)
        recursive_eager(node=self)
        return self

    def _calculate_contiguity(self):
        # https://github.com/numpy/numpy/blob/4c60b3263ac50e5e72f6a909e156314fc3c9cba0/numpy/core/src/multiarray/flagsobject.c#L115
        c_contiguous = f_contiguous = True
        if not self.ndim:
            return c_contiguous, f_contiguous
        nitems = 1
        for i in range(self.ndim-1, -1, -1):
            if self.shape[i] != 1:
                if self.strides[i] != nitems:
                    c_contiguous = False
                nitems *= self.shape[i]
        nitems = 1
        for i in range(self.ndim):
            if self.shape[i] != 1:
                if self.strides[i] != nitems:
                    f_contiguous = False
                nitems *= self.shape[i]
        return c_contiguous, f_contiguous
