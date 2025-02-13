import numpy as np

from core.backend.base import Array
from core.dtype import float32

class NPArray(Array):
    """Wrap numpy ndarray"""
    def __init__(self, data, shape=None, dtype=float32):
        super().__init__(shape, dtype)
        self.data = np.asarray(data, dtype=dtype)
        self.shape = self.data.shape
        self.strides = tuple(s // dtype().itemsize for s in self.data.strides)

    @property
    def size(self): return self.data.nbytes
    def numpy(self): return self.data.copy()

    # ##### Elemwise Ops #####
    def neg(self, out=None): return self.asarray(np.negative(self.data))
    def exp(self, out=None): return self.asarray(np.exp(self.data))
    def log(self, out=None): return self.asarray(np.log(self.data))
    def add(self, other, out=None): return self.asarray(self.data + other.data)
    def sub(self, other, out=None): return self.asarray(self.data - other.data)
    def div(self, other, out=None): return self.asarray(self.data / other.data)
    def mul(self, other, out=None): return self.asarray(self.data * other.data)
    def pow(self, other, out=None): return self.asarray(self.data ** other.data)
    def eq(self, other, out=None): return self.asarray(self.data == other.data)
    def ge(self, other, out=None): return self.asarray(self.data >= other.data)
    def gt(self, other, out=None): return self.asarray(self.data > other.data)
    def matmul(self, other): return self.asarray(self.data @ other.data)
    def relu(self, out=None): return self.asarray(np.maximum(self.data, 0.0))
    def drelu(self, other, out=None): return self.asarray((other.data > 0.0) * self.data)

    # ##### Reduce Ops #####
    def sum(self, axis=None, keepdims=False): return self.asarray(np.sum(self.data, axis=axis, keepdims=keepdims))
    def max(self, axis=None, keepdims=False): return self.asarray(np.max(self.data, axis=axis, keepdims=keepdims))

    # ##### View Ops #####
    def __getitem__(self, key): return self.asarray(self.data[key])
    def __setitem__(self, key, value): self.data[key] = value.data
    def reshape(self, shape): return self.asarray(np.reshape(self.data, shape))
    def expand(self, shape): return self.asarray(np.broadcast_to(self.data, shape))
    def squeeze(self, axis=None): return self.asarray(np.squeeze(self.data, axis))
    def permute(self, axes): return self.asarray(np.transpose(self.data, axes))

    # ##### Creation Ops #####
    @classmethod
    def empty(cls, shape, dtype=float32):
        return cls.asarray(np.empty(shape, dtype))
    @classmethod
    def full(cls, shape, value, dtype=float32):
        return cls.asarray(np.full(shape, value, dtype))
    @classmethod
    def uniform(cls, a, b, shape, dtype=float32):
        return cls.asarray(np.random.uniform(a, b, shape).astype(dtype))
    @classmethod
    def normal(cls, loc, scale, shape, dtype=float32):
        return cls.asarray(np.random.normal(loc, scale, shape).astype(dtype))
