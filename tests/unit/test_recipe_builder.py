def test_dockerfile_content_hash() -> None:
    from mshkn.recipe.builder import dockerfile_content_hash

    h1 = dockerfile_content_hash("FROM mshkn-base\nRUN echo hello")
    h2 = dockerfile_content_hash("FROM mshkn-base\nRUN echo hello")
    h3 = dockerfile_content_hash("FROM mshkn-base\nRUN echo world")
    assert h1 == h2  # deterministic
    assert h1 != h3  # different content
    assert len(h1) == 64  # full SHA256
