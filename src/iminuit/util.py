"""iminuit utility functions and classes."""
import inspect
from collections import OrderedDict
from argparse import Namespace
from collections.abc import Iterable
from . import _repr_html
from . import _repr_text
import numpy as np

inf = float("infinity")


class IMinuitWarning(RuntimeWarning):
    """iminuit warning."""


class HesseFailedWarning(IMinuitWarning):
    """HESSE failed warning."""


class BasicView:
    """Array-like view of parameter state.

    Derived classes need to implement methods _set and _get to access
    specific properties of the parameter state."""

    __slots__ = ("_minuit", "_ndim")

    def __init__(self, minuit, ndim=0):
        self._minuit = minuit
        self._ndim = ndim

    def __iter__(self):
        for i in range(len(self)):
            yield self._get(i)

    def __len__(self):
        return self._minuit.npar

    def __getitem__(self, key):
        if isinstance(key, slice):
            ind = range(*key.indices(len(self)))
            return [self._get(i) for i in ind]
        i = _key2index(self._minuit._var2pos, key)
        return self._get(i)

    def __setitem__(self, key, value):
        self._minuit._copy_state_if_needed()
        if isinstance(key, slice):
            ind = range(*key.indices(len(self)))
            if _ndim(value) == self._ndim:  # basic broadcasting
                for i in ind:
                    self._set(i, value)
            else:
                if len(value) != len(ind):
                    raise ValueError("length of argument does not match slice")
                for i, v in zip(ind, value):
                    self._set(i, v)
        else:
            i = _key2index(self._minuit._var2pos, key)
            self._set(i, value)

    def __eq__(self, other):
        return len(self) == len(other) and all(x == y for x, y in zip(self, other))

    def __repr__(self):
        s = f"<{self.__class__.__name__}"
        for (k, v) in zip(self._minuit._pos2var, self):
            s += f" {k}={v}"
        s += ">"
        return s


def _ndim(obj):
    nd = 0
    while isinstance(obj, Iterable):
        nd += 1
        for x in obj:
            if x is not None:
                obj = x
                break
        else:
            break
    return nd


class ValueView(BasicView):
    """Array-like view of parameter values."""

    def _get(self, i):
        return self._minuit._last_state[i].value

    def _set(self, i, value):
        self._minuit._last_state.set_value(i, value)


class ErrorView(BasicView):
    """Array-like view of parameter errors."""

    def _get(self, i):
        return self._minuit._last_state[i].error

    def _set(self, i, value):
        self._minuit._last_state.set_error(i, value)


class FixedView(BasicView):
    """Array-like view of whether parameters are fixed."""

    def _get(self, i):
        return self._minuit._last_state[i].is_fixed

    def _set(self, i, fix):
        if fix:
            self._minuit._last_state.fix(i)
        else:
            self._minuit._last_state.release(i)


class LimitView(BasicView):
    """Array-like view of parameter limits."""

    def _get(self, i):
        p = self._minuit._last_state[i]
        return (
            p.lower_limit if p.has_lower_limit else -inf,
            p.upper_limit if p.has_upper_limit else inf,
        )

    def _set(self, i, args):
        state = self._minuit._last_state
        val = state[i].value
        err = state[i].error
        # changing limits is a cheap operation, start from clean state
        state.remove_limits(i)
        low, high = _normalize_limit(args)
        if low != -inf and high != inf:  # both must be set
            if low == high:
                state.fix(i)
            else:
                state.set_limits(i, low, high)
        elif low != -inf:  # lower limit must be set
            state.set_lower_limit(i, low)
        elif high != inf:  # lower limit must be set
            state.set_upper_limit(i, high)
        # bug in Minuit2: must set parameter value and error again after changing limits
        if val < low:
            val = low
        elif val > high:
            val = high
        state.set_value(i, val)
        state.set_error(i, err)


def _normalize_limit(lim):
    if lim is None:
        return (-inf, inf)
    lim = list(lim)
    if lim[0] is None:
        lim[0] = -inf
    if lim[1] is None:
        lim[1] = inf
    if lim[0] > lim[1]:
        raise ValueError("limit " + str(lim) + " is invalid")
    return tuple(lim)


class Matrix(np.ndarray):
    """Matrix."""

    __slots__ = ("_var2pos",)

    def __new__(cls, parameters):
        if isinstance(parameters, dict):
            var2pos = parameters
        elif isinstance(parameters, tuple):
            var2pos = {x: i for i, x in enumerate(parameters)}
        else:
            raise TypeError("parameters must be tuple or dict")
        n = len(parameters)
        obj = super(Matrix, cls).__new__(cls, (n, n))
        obj._var2pos = var2pos
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return

        self._var2pos = getattr(obj, "_var2pos")

    def __getitem__(self, key):
        var2pos = self._var2pos
        if isinstance(key, tuple):
            key = tuple((k if isinstance(k, int) else var2pos[k]) for k in key)
        elif isinstance(key, str):
            key = var2pos[key]
        return super(Matrix, self).__getitem__(key)

    def to_table(self):
        names = tuple(self._var2pos)
        nums = _repr_text.matrix_format(self.flatten())
        tab = []
        n = len(self)
        for i, name in enumerate(names):
            tab.append([name] + [nums[n * i + j] for j in range(n)])
        return tab, names

    def correlation(self):
        a = self.copy()
        d = np.diag(a) ** 0.5
        a /= np.outer(d, d) + 1e-100
        return a

    def __repr__(self):
        return super(Matrix, self).__str__()

    def __str__(self):
        return repr(self)

    def _repr_html_(self):
        return _repr_html.matrix(self)

    def _repr_pretty_(self, p, cycle):
        if cycle:
            p.text("<Matrix ...>")
        else:
            p.text(_repr_text.matrix(self))


class FMin:
    """Function minimum view."""

    __slots__ = (
        "_src",
        "_has_parameters_at_limit",
        "_nfcn",
        "_ngrad",
        "_edm_goal",
    )

    def __init__(self, fmin, nfcn, ngrad, edm_goal):
        self._src = fmin
        self._has_parameters_at_limit = False
        for mp in fmin.state:
            if mp.is_fixed or not mp.has_limits:
                continue
            v = mp.value
            e = mp.error
            lb = mp.lower_limit if mp.has_lower_limit else -inf
            ub = mp.upper_limit if mp.has_upper_limit else inf
            # the 0.5 error threshold is somewhat arbitrary
            self._has_parameters_at_limit |= min(v - lb, ub - v) < 0.5 * e
        self._nfcn = nfcn
        self._ngrad = ngrad
        self._edm_goal = edm_goal

    @property
    def edm(self):
        """Estimated Distance to Minimum.

        Minuit uses this criterion to determine whether the fit converged. It depends
        on the gradient and the Hessian matrix. It measures how well the current
        second order expansion around the function minimum describes the function, by
        taking the difference between the predicted (based on gradient and Hessian)
        function value at the minimum and the actual value.
        """
        return self._src.edm

    @property
    def edm_goal(self):
        """EDM threshold value for stopping the minimization. The threshold is allowed
        to be violated up to a factor of 10 in some situations."""
        return self._edm_goal

    @property
    def fval(self):
        """Value of the cost function at the minimum."""
        return self._src.fval

    @property
    def has_parameters_at_limit(self):
        """Whether any bounded parameter was fitted close to a bound.

        The estimated error for the affected parameters is usually off. May be an
        indication to remove or loosen the limits on the affected parameter.
        """
        return self._has_parameters_at_limit

    @property
    def nfcn(self):
        """Number of function calls so far."""
        return self._nfcn

    @property
    def ngrad(self):
        """Number of function gradient calls so far."""
        return self._ngrad

    @property
    def is_valid(self):
        """Whether Migrad converged successfully.

        For it to return True, the following conditions need to be fulfilled:
        - has_valid_parameters is True
        - has_reached_call_limit is False
        - is_above_max_edm is False

        Note: The actual verdict is computed inside the Minuit2 C++ code, so we
        cannot guarantee that is_valid is exactly equivalent to these conditions.
        """
        return self._src.is_valid

    @property
    def has_valid_parameters(self):
        """Whether parameters are valid.

        For it to return True, the following conditions need to be fulfilled:
        - has_reached_call_limit is False
        - is_above_max_edm is False

        Note: The actual verdict is computed inside the Minuit2 C++ code, so we
        cannot guarantee that is_valid is exactly equivalent to these conditions.
        """
        return self._src.has_valid_parameters

    @property
    def has_accurate_covar(self):
        """Whether the covariance matrix is accurate.

        While Migrad runs, it computes an approximation to the current Hessian
        matrix. If the strategy is set to 0 or if the fit did not converge, the
        inverse of this approximation is returned instead of the inverse of the
        accurately computed Hessian matrix. This property returns False if the
        approximation has been returned instead of an accurate matrix.
        """
        return self._src.has_accurate_covar

    @property
    def has_posdef_covar(self):
        """Whether the Hessian matrix is positive definite.

        This must be the case if the extremum is a minimum. Otherwise it is a
        maximum or a saddle point.

        If the fit has converged, this should always be true. It may be false if the
        fit did not converge or was stopped prematurely.
        """
        return self._src.has_posdef_covar

    @property
    def has_made_posdef_covar(self):
        """Whether the matrix was forced to be positive definite.

        While Migrad runs, it computes an approximation to the current Hessian matrix.
        It can happen that this approximation is not positive definite, but that is
        required to compute the next Newton step. Migrad then adds an appropriate
        diagonal matrix to enforce positive definiteness.

        If the fit has converged, this should always be false. It may be true if the
        fit did not converge or was stopped prematurely.
        """
        return self._src.has_made_posdef_covar

    @property
    def hesse_failed(self):
        """Whether the last call to Hesse failed."""
        return self._src.hesse_failed

    @property
    def has_covariance(self):
        """Whether a covariance matrix was computed at all.

        This is false if the Simplex minimization algorithm was used instead of
        Migrad, in which no approximation to the Hessian is computed.
        """
        return self._src.has_covariance

    @property
    def is_above_max_edm(self):
        """Whether the EDM value is below the convergence threshold.

        If this is true, the fit did not converge; otherwise this is false.
        """
        return self._src.is_above_max_edm

    @property
    def has_reached_call_limit(self):
        """Whether Migrad exceeded the allowed number of function calls.

        If this is true, the fit was stopped before convergence was reached.
        """
        return self._src.has_reached_call_limit

    @property
    def up(self):
        """Equal to the value of ``Minuit.errordef`` when Migrad ran."""
        return self._src.up

    def __repr__(self):
        s = "<FMin"
        for key in sorted(dir(self)):
            if key.startswith("_"):
                continue
            val = getattr(self, key)
            s += f" {key}={val!r}"
        s += ">"
        return s

    def _repr_html_(self):
        return _repr_html.fmin(self)

    def _repr_pretty_(self, p, cycle):
        if cycle:
            p.text("<FMin ...>")
        else:
            p.text(_repr_text.fmin(self))


class Param:
    """Data object for a single Parameter."""

    __slots__ = (
        "number",
        "name",
        "value",
        "error",
        "merror",
        "is_const",
        "is_fixed",
        "has_limits",
        "has_lower_limit",
        "has_upper_limit",
        "lower_limit",
        "upper_limit",
    )

    def __init__(self, *args):
        assert len(args) == len(self.__slots__)
        for k, arg in zip(self.__slots__, args):
            setattr(self, k, arg)

    def __eq__(self, other):
        return all(getattr(self, k) == getattr(other, k) for k in self.__slots__)

    def __repr__(self):
        pairs = []
        for k in self.__slots__:
            v = getattr(self, k)
            pairs.append(f"{k}={v!r}")
        return "Param(" + ", ".join(pairs) + ")"

    def _repr_pretty_(self, p, cycle):
        if cycle:
            p.text("Param(...)")
        else:
            p.text(_repr_text.params([self]))


class Params(tuple):
    """Tuple-like holder of parameter data objects."""

    __slots__ = ()

    def _repr_html_(self):
        return _repr_html.params(self)

    def to_table(self):
        header = [
            "pos",
            "name",
            "value",
            "error",
            "error-",
            "error+",
            "limit-",
            "limit+",
            "fixed",
        ]
        tab = []
        for i, mp in enumerate(self):
            name = mp.name
            row = [i, name]
            me = mp.merror
            if me:
                val, err, mel, meu = _repr_text.pdg_format(mp.value, mp.error, *me)
            else:
                val, err = _repr_text.pdg_format(mp.value, mp.error)
                mel = ""
                meu = ""
            row += [
                val,
                err,
                mel,
                meu,
                f"{mp.lower_limit}" if mp.lower_limit is not None else "",
                f"{mp.upper_limit}" if mp.upper_limit is not None else "",
                "yes" if mp.is_fixed else "",
            ]
            tab.append(row)
        return tab, header

    def __getitem__(self, key):
        if isinstance(key, str):
            for i, p in enumerate(self):
                if p.name == key:
                    break
            key = i
        return super(Params, self).__getitem__(key)

    def _repr_pretty_(self, p, cycle):
        if cycle:
            p.text("Params(...)")
        else:
            p.text(_repr_text.params(self))


class MError:
    """Minos result object."""

    __slots__ = (
        "number",
        "name",
        "lower",
        "upper",
        "is_valid",
        "lower_valid",
        "upper_valid",
        "at_lower_limit",
        "at_upper_limit",
        "at_lower_max_fcn",
        "at_upper_max_fcn",
        "lower_new_min",
        "upper_new_min",
        "nfcn",
        "min",
    )

    def __init__(self, *args):
        assert len(args) == len(self.__slots__)
        for k, arg in zip(self.__slots__, args):
            setattr(self, k, arg)

    def __eq__(self, other):
        return all(getattr(self, k) == getattr(other, k) for k in self.__slots__)

    def __repr__(self):
        s = "<MError"
        for idx, k in enumerate(self.__slots__):
            v = getattr(self, k)
            s += f" {k}={v!r}"
        s += ">"
        return s

    def _repr_html_(self):
        return _repr_html.merrors({None: self})

    def _repr_pretty_(self, p, cycle):
        if cycle:
            p.text("<MError ...>")
        else:
            p.text(_repr_text.merrors({None: self}))


class MErrors(OrderedDict):
    """Dict-like map from parameter name to Minos result object."""

    __slots__ = ()

    def _repr_html_(self):
        return _repr_html.merrors(self)

    def __repr__(self):
        return "<MErrors\n  " + ",\n  ".join(repr(x) for x in self.values()) + "\n>"

    def _repr_pretty_(self, p, cycle):
        if cycle:
            p.text("<MErrors ...>")
        else:
            p.text(_repr_text.merrors(self))

    def __getitem__(self, key):
        if isinstance(key, int):
            if key < 0:
                key += len(self)
            if key < 0 or key >= len(self):
                raise IndexError("index out of range")
            for i, k in enumerate(self):
                if i == key:
                    break
            key = k
        return OrderedDict.__getitem__(self, key)


def make_func_code(params):
    """Make a func_code object to fake function signature.

    You can make a funccode from describable object by::

        make_func_code(["x", "y"])
    """
    return Namespace(co_varnames=params, co_argcount=len(params))


def describe(callable):
    """Attempt to extract the function argument names.

    **Returns**

    Returns a list of strings with the parameters names if successful. If unsuccessful,
    it returns an empty list.

    **Function signature extraction algorithm**

    Parameter names are extracted with the following three methods, which are attempted
    in order. The first to succeed determines the result.

    1. Using ``obj.func_code``. If an objects has a ``func_code`` attribute, it is used
       to detect the parameters. Examples::

           def f(x, y):
               return (x - 2) ** 2 + (y - 3) ** 2

           f = lambda x, y: (x - 2) ** 2 + (y - 3) ** 2

       Users are encouraged to use this mechanism to provide signatures for objects that
       otherwise would not have a detectable signature. The function
       :func:`make_func_code` can be used to generate an appropriate func_code object.
       An example where this is useful is shown in one of the tutorials.

    2. Using :func:`inspect.signature`. The :mod:`inspect` module provides a general
       function to extract the signature of a Python callable. It works on most
       callables, including Functors like this::

        class MyLeastSquares:
            def __call__(self, a, b):
                # ...

    3. Using the docstring. The docstring is textually parsed to detect the parameter
       names. This requires that a docstring is present which follows the Python
       standard formatting for function signatures.

    **Handling of ambiguous cases**

    How functions with positional and keyword argument are handled::

        # describe returns [a, b];
        # *args and **kwargs are ignored
        def fcn(a, b, *args, **kwargs): ...

        # describe returns [a, b, c];
        # positional arguments with default values are detected
        def fcn(a, b, c=1): ...

    """
    return (
        _arguments_from_func_code(callable)
        or _arguments_from_inspect(callable)
        or _arguments_from_docstring(callable)
    )


def _arguments_from_func_code(obj):
    # Check (faked) f.func_code; for backward-compatibility with iminuit-1.x
    if hasattr(obj, "func_code"):
        fc = obj.func_code
        return tuple(fc.co_varnames[: fc.co_argcount])
    return ()


def _arguments_from_inspect(obj):
    try:
        # fails for builtin on Windows and OSX in Python 3.6
        signature = inspect.signature(obj)
    except ValueError:
        return ()

    args = []
    for name, par in signature.parameters.items():
        # stop when variable number of arguments is encountered
        if par.kind is inspect.Parameter.VAR_POSITIONAL:
            break
        # stop when keyword argument is encountered
        if par.kind is inspect.Parameter.VAR_KEYWORD:
            break
        args.append(name)
    return tuple(args)


def _arguments_from_docstring(obj):
    doc = inspect.getdoc(obj)

    if doc is None:
        return ()

    # Examples of strings we want to parse:
    #   min(iterable, *[, default=obj, key=func]) -> value
    #   min(arg1, arg2, *args, *[, key=func]) -> value
    #   Foo.bar(self, int ncall_me =10000, [resume=True, int nsplit=1])

    name = obj.__name__

    token = name + "("
    start = doc.find(token)
    if start < 0:
        return ()
    start += len(token)

    nbrace = 1
    for ich, ch in enumerate(doc[start:]):
        if ch == "(":
            nbrace += 1
        elif ch == ")":
            nbrace -= 1
        if nbrace == 0:
            break
    items = [x.strip(" []") for x in doc[start : start + ich].split(",")]

    if items[0] == "self":
        items = items[1:]

    #   "iterable", "*", "default=obj", "key=func"
    #   "arg1", "arg2", "*args", "*", "key=func"
    #   "int ncall_me =10000", "resume=True", "int nsplit=1"

    try:
        i = items.index("*args")
        items = items[:i]
    except ValueError:
        pass

    #   "iterable", "*", "default=obj", "key=func"
    #   "arg1", "arg2", "*", "key=func"
    #   "int ncall_me =10000", "resume=True", "int nsplit=1"

    def extract(s):
        a = s.find(" ")
        b = s.find("=")
        if a < 0:
            a = 0
        if b < 0:
            b = len(s)
        return s[a:b].strip()

    items = [extract(x) for x in items if x != "*"]

    #   "iterable", "default", "key"
    #   "arg1", "arg2", "key"
    #   "ncall_me", "resume", "nsplit"

    return tuple(items)


def _guess_initial_step(val):
    return 1e-2 * val if val != 0 else 1e-1  # heuristic


def _key2index(var2pos, key):
    if isinstance(key, int):
        i = key
        if i < 0:
            i += len(var2pos)
        if i < 0 or i >= len(var2pos):
            raise IndexError
    else:
        i = var2pos[key]
    return i
