from pathlib import Path

import pytest


def test_dockerfile_content_hash():
    from mshkn.recipe.builder import dockerfile_content_hash

    h1 = dockerfile_content_hash("FROM mshkn-base\nRUN echo hello")
    h2 = dockerfile_content_hash("FROM mshkn-base\nRUN echo hello")
    h3 = dockerfile_content_hash("FROM mshkn-base\nRUN echo world")
    assert h1 == h2  # deterministic
    assert h1 != h3  # different content
    assert len(h1) == 64  # full SHA256


@pytest.mark.asyncio
async def test_ensure_base_image_already_exists():
    from unittest.mock import AsyncMock, patch

    from mshkn.config import Config
    from mshkn.recipe.builder import ensure_base_image

    config = Config(ssh_key_path=Path("/tmp/test-key"))
    with patch("mshkn.recipe.builder.run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = ""  # docker image inspect succeeds
        await ensure_base_image(config)
        mock_run.assert_called_once_with("docker image inspect mshkn-base", check=True)
