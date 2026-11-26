"""Test-only compatibility shim for environments without `streamlit` installed (BUG-19).

Security & Reliability Rules (BUG-19):
1. Activated ONLY in explicit test mode (`pytest` active or `COINAGENT_TEST_MODE=1`).
2. Uses specific module spec lookup (`importlib.util.find_spec`), NEVER broad
   `except Exception:` that could mask a corrupted production Streamlit installation.
3. In production (`test_mode=False`), importing `streamlit` always uses the real
   installed package directly and never substitutes the test shim.
"""
import contextlib
import importlib.util
import os
import runpy
import sys
import types
from pathlib import Path


def is_test_environment(explicit_test_mode=None):
    """Return True only when running inside an automated test runner."""
    if explicit_test_mode is not None:
        return bool(explicit_test_mode)
    return bool(
        "pytest" in sys.modules
        or os.getenv("PYTEST_CURRENT_TEST")
        or str(os.getenv("COINAGENT_TEST_MODE", "")).lower() in ("1", "true", "yes", "on")
    )


class _RerunSignal(Exception):
    pass


class StreamlitWidgetAlreadyInstantiatedError(RuntimeError):
    pass


class _SessionStateProxy(dict):
    def __init__(self, runner):
        super().__init__()
        self._runner = runner

    def __setitem__(self, key, value):
        if key in self._runner._instantiated_widget_keys:
            raise StreamlitWidgetAlreadyInstantiatedError(
                f"Cannot modify session_state['{key}'] after widget with key '{key}' has already been instantiated."
            )
        super().__setitem__(key, value)
        if key in self._runner._widget_values or str(key).startswith("cfg_"):
            self._runner._widget_values[key] = value


class _Element:
    def __init__(self, runner, kind, label="", value=None, options=None, key=None, **kwargs):
        self._runner = runner
        self.kind = kind
        self.label = str(label)
        self.value = value
        self.options = list(options) if options is not None else []
        self.key = key or f"{kind}:{self.label}"
        self.type = kwargs.get("type", "default")
        for k, v in kwargs.items():
            setattr(self, k, v)

    def set_value(self, new_val):
        self._runner._widget_values[self.key] = new_val
        self.value = new_val
        return self

    def check(self):
        return self.set_value(True)

    def uncheck(self):
        return self.set_value(False)

    def click(self):
        self._runner._clicked_buttons.add(self.key)
        return self

    def run(self, timeout=None):
        return self._runner.run(timeout=timeout)


class _ContainerContext:
    def __init__(self, mod):
        self._mod = mod

    def __enter__(self):
        return self._mod

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, name):
        return getattr(self._mod, name)


class _AppTestRunner:
    def __init__(self, script_path, default_timeout=20):
        self.script_path = str(Path(script_path).resolve())
        self.default_timeout = default_timeout
        self._instantiated_widget_keys = set()
        self._widget_values = {}
        self.session_state = _SessionStateProxy(self)
        self.secrets = {}
        self._clicked_buttons = set()
        self._reset_elements()
        self.exception = None

    def _reset_elements(self):
        self._instantiated_widget_keys.clear()
        self.text_input = []
        self.number_input = []
        self.selectbox = []
        self.select_slider = []
        self.toggle = []
        self.checkbox = []
        self.date_input = []
        self.button = []
        self.download_button = []
        self.tabs = []
        self.metric = []
        self.info = []
        self.warning = []
        self.error = []
        self.success = []
        self.caption = []
        self.markdown = []
        self.write = []
        self.dataframe = []
        self.json = []
        self.code = []

    def get(self, kind):
        return getattr(self, kind, [])

    def run(self, timeout=None):
        st_mod = sys.modules["streamlit"]
        st_mod._bind_runner(self)
        self.exception = None
        for _ in range(3):
            self._reset_elements()
            try:
                runpy.run_path(self.script_path, run_name="__main__")
                break
            except _RerunSignal:
                self._clicked_buttons.clear()
                continue
            except Exception as exc:
                self.exception = exc
                break
        self._clicked_buttons.clear()
        return self


def _build_streamlit_module():
    st = types.ModuleType("streamlit")
    st.__path__ = []
    st.__is_test_shim__ = True
    st.session_state = {}
    st.secrets = {}
    st._active_runner = None
    st._resource_cache = {}

    def _bind_runner(runner):
        st._active_runner = runner
        st.session_state = runner.session_state
        st.secrets = runner.secrets

    st._bind_runner = _bind_runner

    def _rec(list_name, label="", value=None, **kwargs):
        r = st._active_runner
        if r is None:
            return _Element(None, list_name, label=label, value=value, **kwargs)
        el = _Element(r, list_name, label=label, value=value, **kwargs)
        getattr(r, list_name).append(el)
        return el

    def _wval(kind, label, default, key=None):
        r = st._active_runner
        wkey = key or f"{kind}:{label}"
        if r is not None and key:
            r._instantiated_widget_keys.add(key)
        if r is not None and wkey in r._widget_values:
            val = r._widget_values[wkey]
            if key:
                dict.__setitem__(r.session_state, key, val)
            return val, wkey
        if r is not None:
            r._widget_values[wkey] = default
            if key:
                dict.__setitem__(r.session_state, key, default)
        return default, wkey

    st.set_page_config = lambda **kwargs: None
    st.title = lambda text, **kwargs: _rec("markdown", value=str(text))
    st.subheader = lambda text, **kwargs: _rec("markdown", value=str(text))
    st.markdown = lambda text, **kwargs: _rec("markdown", value=str(text))
    st.write = lambda *args, **kwargs: _rec("write", value=" ".join(str(a) for a in args))
    st.caption = lambda text, **kwargs: _rec("caption", value=str(text))
    st.info = lambda text, **kwargs: _rec("info", value=str(text))
    st.warning = lambda text, **kwargs: _rec("warning", value=str(text))
    st.error = lambda text, **kwargs: _rec("error", value=str(text))
    st.success = lambda text, **kwargs: _rec("success", value=str(text))
    st.divider = lambda **kwargs: None
    st.dataframe = lambda data, **kwargs: _rec("dataframe", value=data)
    st.json = lambda data, **kwargs: _rec("json", value=data)
    st.code = lambda body, **kwargs: _rec("code", value=str(body))
    st.plotly_chart = lambda fig, **kwargs: None

    def metric(label, value, **kwargs):
        return _rec("metric", label=str(label), value=str(value))

    def text_input(label, value="", key=None, type="default", **kwargs):
        val, wkey = _wval("text_input", label, value, key)
        _rec("text_input", label=label, value=val, key=wkey, type=type)
        return val

    def number_input(label, min_value=None, max_value=None, value=0.0, step=None, key=None, **kwargs):
        val, wkey = _wval("number_input", label, value, key)
        _rec("number_input", label=label, value=val, key=wkey)
        return val

    def date_input(label, value=None, key=None, **kwargs):
        val, wkey = _wval("date_input", label, value, key)
        _rec("date_input", label=label, value=val, key=wkey)
        return val

    def checkbox(label, value=False, key=None, **kwargs):
        val, wkey = _wval("checkbox", label, bool(value), key)
        _rec("checkbox", label=label, value=bool(val), key=wkey)
        return bool(val)

    def toggle(label, value=False, key=None, **kwargs):
        val, wkey = _wval("toggle", label, bool(value), key)
        _rec("toggle", label=label, value=bool(val), key=wkey)
        return bool(val)

    def selectbox(label, options, index=0, format_func=str, key=None, **kwargs):
        opts = list(options)
        default = opts[index] if opts else None
        val, wkey = _wval("selectbox", label, default, key)
        formatted_opts = [str(format_func(o)) for o in opts]
        _rec("selectbox", label=label, value=val, options=formatted_opts, raw_options=opts, key=wkey)
        return val

    def select_slider(label, options, value=None, format_func=str, key=None, **kwargs):
        opts = list(options)
        default = value if value is not None else (opts[0] if opts else None)
        val, wkey = _wval("select_slider", label, default, key)
        _rec("select_slider", label=label, value=val, options=[str(format_func(o)) for o in opts], key=wkey)
        return val

    def button(label, key=None, **kwargs):
        r = st._active_runner
        wkey = key or f"button:{label}"
        clicked = bool(r is not None and wkey in r._clicked_buttons)
        _rec("button", label=label, value=clicked, key=wkey)
        return clicked

    def form_submit_button(label, key=None, **kwargs):
        return button(label, key=key, **kwargs)

    def download_button(label, data, file_name="", mime="", key=None, **kwargs):
        wkey = key or f"download_button:{label}"
        _rec("download_button", label=label, value=data, file_name=file_name, mime=mime, key=wkey)
        return False

    def columns(spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [_ContainerContext(st) for _ in range(count)]

    def tabs(labels, **kwargs):
        out = []
        for lbl in labels:
            _rec("tabs", label=str(lbl), value=str(lbl))
            out.append(_ContainerContext(st))
        return out

    def expander(label, **kwargs):
        return _ContainerContext(st)

    def form(key, **kwargs):
        return _ContainerContext(st)

    @contextlib.contextmanager
    def spinner(text=""):
        yield

    def cache_resource(func=None, **kwargs):
        def decorator(fn):
            def wrapper(*args, **kw):
                ckey = (fn.__name__, repr(args), repr(sorted(kw.items())))
                if ckey not in st._resource_cache:
                    st._resource_cache[ckey] = fn(*args, **kw)
                return st._resource_cache[ckey]
            return wrapper
        return decorator(func) if callable(func) else decorator

    def fragment(func=None, **kwargs):
        def decorator(fn):
            return fn
        return decorator(func) if callable(func) else decorator

    def rerun():
        raise _RerunSignal()

    st.metric = metric
    st.text_input = text_input
    st.number_input = number_input
    st.date_input = date_input
    st.checkbox = checkbox
    st.toggle = toggle
    st.selectbox = selectbox
    st.select_slider = select_slider
    st.button = button
    st.form_submit_button = form_submit_button
    st.download_button = download_button
    st.columns = columns
    st.tabs = tabs
    st.expander = expander
    st.form = form
    st.spinner = spinner
    st.cache_resource = cache_resource
    st.fragment = fragment
    st.rerun = rerun
    st.sidebar = _ContainerContext(st)

    testing_mod = types.ModuleType("streamlit.testing")
    testing_mod.__path__ = []
    v1_mod = types.ModuleType("streamlit.testing.v1")

    class AppTest:
        @staticmethod
        def from_file(script_path, default_timeout=20):
            return _AppTestRunner(script_path, default_timeout=default_timeout)

    v1_mod.AppTest = AppTest
    testing_mod.v1 = v1_mod
    st.testing = testing_mod
    return st, testing_mod, v1_mod


def _build_plotly_modules():
    plotly_mod = types.ModuleType("plotly")
    plotly_mod.__path__ = []
    go_mod = types.ModuleType("plotly.graph_objects")

    class _DummyTrace:
        def __init__(self, *args, **kwargs):
            pass

    class _DummyFigure:
        def __init__(self, *args, **kwargs):
            self.traces = []
        def add_trace(self, *args, **kwargs):
            self.traces.append((args, kwargs))
        def add_hline(self, *args, **kwargs):
            pass
        def update_layout(self, *args, **kwargs):
            pass

    go_mod.Figure = _DummyFigure
    go_mod.Candlestick = _DummyTrace
    go_mod.Scatter = _DummyTrace
    plotly_mod.graph_objects = go_mod
    return plotly_mod, go_mod


def install_if_missing(test_mode=None):
    """BUG-19: Install test shim ONLY in test mode and ONLY when module spec is genuinely absent."""
    if not is_test_environment(test_mode):
        return False

    existing_st = sys.modules.get("streamlit")
    if existing_st is None and importlib.util.find_spec("streamlit") is None:
        st, testing_mod, v1_mod = _build_streamlit_module()
        sys.modules["streamlit"] = st
        sys.modules["streamlit.testing"] = testing_mod
        sys.modules["streamlit.testing.v1"] = v1_mod

    existing_pl = sys.modules.get("plotly")
    if existing_pl is None and importlib.util.find_spec("plotly") is None:
        plotly_mod, go_mod = _build_plotly_modules()
        sys.modules["plotly"] = plotly_mod
        sys.modules["plotly.graph_objects"] = go_mod

    return True
