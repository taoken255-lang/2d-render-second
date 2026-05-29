"""
Error classification heuristic for engine failures.

Maps engine error messages and stderr tails to stable error codes that
workers and producers can programmatically handle.

Standard error codes:
- RESOURCE_EXHAUSTED: OOM, CUDA errors, SIGKILL (likely OOM)
- BAD_INPUTS: Invalid media files, missing files
- INVALID_INPUT_URL: Presigned URL issues (403, 404, expired signature)
- BAD_PARAMS: Invalid hyperparameters, unrecognized arguments
- ENGINE_ERROR: Generic inference failure (fallback)
- CANCELLED: User-requested cancellation (set by adapter, not heuristic)
- DEADLINE_EXCEEDED: Job timeout (set by adapter, not heuristic)
"""


def classify_engine_error(msg: str, tail: str) -> str:
    """
    Map engine failures to stable error codes using message + stderr tail.

    Checks for known error patterns (case-insensitive) and returns a stable code.
    Used by adapters to populate JobRec.error["code"] field.

    Args:
        msg: Exception message or error summary
        tail: Last 50-200 lines of engine stderr (from RunnerFailed.log_tail)

    Returns:
        Error code string (RESOURCE_EXHAUSTED, BAD_INPUTS, etc.)

    Example:
        try:
            await runner.run_engine(...)
        except RunnerFailed as e:
            code = classify_engine_error(str(e), e.log_tail)
            rec.error = {"code": code, "message": str(e)}
    """
    text = f"{msg}\n{tail}".lower()

    # SIGKILL (exit code -9) often indicates OOM kill
    if "exit code -9" in text or "signal 9" in text or "sigkill" in text:
        # Check if loading models when killed (likely VRAM exhaustion)
        if "loading models" in text or "torch" in text or "cuda" in text:
            return "RESOURCE_EXHAUSTED"
        return "RESOURCE_EXHAUSTED"  # Default SIGKILL to resource issue

    # Resource exhaustion / CUDA
    oom_markers = (
        "out of memory", "cuda error", "cublas_status_alloc_failed", "cudnn",
        "torch.cuda.outofmemoryerror", "failed to allocate", "memory allocation failed",
    )
    if any(k in text for k in oom_markers):
        return "RESOURCE_EXHAUSTED"

    # Bad inputs (media issues / invalid presigned URLs)
    bad_input_markers = (
        "no such file or directory", "invalid data found when processing input",
        "is not a valid image", "cannot open", "failed to read", "unsupported codec",
        "error opening", "wav:", "png:", "probe unexpected status", "presigned",
        "signature", "access denied", "403", "404",
    )
    if any(k in text for k in bad_input_markers):
        # For URL issues, prefer INVALID_INPUT_URL code
        if "probe unexpected status" in text or "403" in text or "404" in text or "access denied" in text:
            return "INVALID_INPUT_URL"
        return "BAD_INPUTS"

    # Bad params (argparse/hparams/yaml)
    bad_param_markers = (
        "unrecognized arguments", "invalid literal", "valueerror", "typeerror",
        "keyerror", "must be one of", "unsupported", "bad params", "expected", "yaml", "hparams",
    )
    if any(k in text for k in bad_param_markers):
        return "BAD_PARAMS"

    # NCCL/distributed failures (treat as engine error in single-GPU mode)
    if "nccl" in text or "init_process_group" in text:
        return "ENGINE_ERROR"

    # Fallback: generic engine error
    return "ENGINE_ERROR"
