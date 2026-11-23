# Container profile for Code-node execution (SANDBOX_RUNNER=container).
#
# Build (from `backend/`, because the COPY paths are relative to it):
#
#     docker build -f docker/sandbox.Dockerfile -t agent-scaffold-sandbox:latest .
#
# ---------------------------------------------------------------------------
# UNVERIFIED ON THIS MACHINE
# ---------------------------------------------------------------------------
# The Docker daemon was not running when this file was written, so it has never
# been built. The *flags* `ContainerSandboxRunner` passes are tested without a
# daemon (`tests/test_capability_layer.py::TestContainerCommandShape`), but the
# image itself is unproven. Build it once and fix whatever falls out before
# trusting `SANDBOX_RUNNER=container` in any real deployment.
#
# ---------------------------------------------------------------------------
# Why four files and not the whole `app` package
# ---------------------------------------------------------------------------
# A Code node's snippet needs the AST guard, the builtin allowlist, and the
# stdin/stdout protocol. Nothing else. Copying `app/` wholesale would put the
# API, the database layer, and the settings loader — and therefore the operator's
# API key — inside the sandbox, which is a larger attack surface and a larger
# image for no benefit.
#
# The cost of the smaller copy is that this list is hand-written and can drift
# from the real import graph. If `sandbox_child.py` ever imports something new
# from the package, add it here or the container will fail with
# `ModuleNotFoundError` at runtime. The bootstrap asserts the package is
# importable, but nothing can assert the file list is complete.
#
# `python:3.13-slim` is not pinned by digest. Pin it (`@sha256:...`) for a
# production deployment; leaving it floating keeps a scaffold easy to update.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The package skeleton. `sandbox_child` imports `sandbox`, which imports only
# the standard library — no third-party dependencies are installed on purpose.
COPY app/__init__.py /workspace/app/__init__.py
COPY app/runtime/__init__.py /workspace/app/runtime/__init__.py
COPY app/runtime/sandbox.py /workspace/app/runtime/sandbox.py
COPY app/runtime/sandbox_child.py /workspace/app/runtime/sandbox_child.py

# Belt and braces with `--read-only` at run time: an image that is not writable
# cannot become writable because someone forgot the flag.
RUN chmod -R a-w /workspace && chmod -R a+rX /workspace

WORKDIR /workspace

# `--user 65534:65534` is passed by the runner as well. Setting it here means a
# hand-run `docker run` of this image is also unprivileged.
USER 65534:65534

# No CMD: the runner supplies the command, because
# `ContainerSandboxRunner.entrypoint()` derives the `sys.path` bootstrap from the
# configured working directory. Baking a copy here would create two definitions
# that could disagree.
