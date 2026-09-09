"""Host agent that runs Open-Inspect sandboxes on an on-premise Mac.

The control plane's macOS provider is a thin translation layer over the HTTP
surface in `app`. Everything platform-specific lives behind the `SandboxDriver`
seam in `driver`: this slice ships `LocalProcessDriver`, which runs the sandbox
runtime as a plain process in a scratch directory, and the Tart driver replaces
it later without the service or the provider changing.
"""
