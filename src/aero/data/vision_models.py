"""Vision model list for Aero."""

VISION_MODELS = [
    ("qwen3.5-flash", "Qwen3.5 Flash"),
    ("qwen3.5-plus", "Qwen3.5 Plus"),
    ("qwen3.6-flash", "Qwen3.6 Flash"),
    ("qwen3.6-plus", "Qwen3.6 Plus"),
    ("qwen3.7-plus", "Qwen3.7 Plus"),
    ("qwen3-vl-plus", "Qwen3-VL Plus"),
    ("qwen3-vl-flash", "Qwen3-VL Flash"),
    ("qwen-vl-max", "Qwen-VL Max"),
    ("qwen-vl-plus", "Qwen-VL Plus"),
]


def vision_model_options() -> list[tuple[str, str]]:
    return list(VISION_MODELS)


def is_valid_vision_model(model: str) -> bool:
    ids = {vid for vid, _ in VISION_MODELS}
    return model.strip() in ids or model.strip().lower() in {v.lower() for v in ids}
