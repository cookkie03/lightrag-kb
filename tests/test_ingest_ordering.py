import ingest


def test_ordered_categories_prioritizes_text_before_audio():
    plan = [
        {"category": "audio", "needs_render": True, "needs_insert": True},
        {"category": "pdf", "needs_render": True, "needs_insert": True},
        {"category": "text", "needs_render": True, "needs_insert": True},
        {"category": "office", "needs_render": True, "needs_insert": True},
        {"category": "images", "needs_render": True, "needs_insert": True},
    ]

    assert ingest._ordered_categories(plan) == ["text", "office", "images", "pdf", "audio"]
