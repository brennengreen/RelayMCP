from relaymcp.device.text import chunks, speakable


def test_speakable_strips_markdown():
    out = speakable("**Done!** Run `ls` or see [the docs](https://x.y/z).\n- one\n- two\n```\ncode\n```")
    assert out == "Done! Run ls or see the docs. one two"


def test_speakable_truncates_at_sentence():
    out = speakable("First sentence here. " * 40, limit=100)
    assert out.endswith("The rest is on screen.") and len(out) < 140


def test_chunks_first_piece_is_short():
    text = ("This is a long sentence that should be split, because the first piece must start playing quickly, "
            "well before the rest is ready.")
    parts = chunks(text)
    assert parts[0].endswith(",") and len(parts[0]) <= 80
    assert " ".join(parts) == text


def test_chunks_keep_short_first_sentence_and_fold_fragments():
    assert chunks("Okay. I opened Minecraft. It's loading now; give it a minute! Want me to join?") == [
        "Okay.", "I opened Minecraft. It's loading now; give it a minute!", "Want me to join?"]
    assert chunks("Done.") == ["Done."]
