"""dockerfile_base_image: the last FROM decides what gets exported, so it decides the rule."""

from __future__ import annotations

import pytest

from mshkn.services.recipes import dockerfile_base_image, image_name


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM mshkn-base\nRUN true", "mshkn-base"),
        ("from mshkn-base:latest as base\nRUN true", "mshkn-base:latest"),
        (
            "# syntax=docker/dockerfile:1\n\nARG TAG=latest\nFROM mshkn-base:${TAG}\n",
            "mshkn-base:${TAG}",
        ),
        ("FROM --platform=linux/amd64 mshkn-base AS build\nRUN true", "mshkn-base"),
        (
            "FROM golang:1.22 AS build\nRUN go build\nFROM mshkn-base\nCOPY --from=build /a /a",
            "mshkn-base",
        ),
        ("FROM mshkn-base AS base\nFROM scratch\nCOPY --from=base / /", "scratch"),
        ("FROM python:3.12\nRUN true", "python:3.12"),
        ("  FROM   ubuntu@sha256:abc  ", "ubuntu@sha256:abc"),
        ("RUN true\n# FROM mshkn-base in a comment does not count", None),
        ("", None),
        ("FROM \\\n    mshkn-base\nRUN true", "mshkn-base"),
        (
            "FROM --platform=linux/amd64 \\\n  mshkn-base:latest \\\n  AS base\nRUN true",
            "mshkn-base:latest",
        ),
        ("FROM python:3.12\nFROM mshkn-base \\", "mshkn-base"),
        ("FROM mshkn-base\nRUN true \\", "mshkn-base"),
    ],
)
def test_last_from_wins(dockerfile: str, expected: str | None) -> None:
    assert dockerfile_base_image(dockerfile) == expected


@pytest.mark.parametrize(
    ("reference", "name"),
    [
        ("mshkn-base", "mshkn-base"),
        ("mshkn-base:latest", "mshkn-base"),
        ("mshkn-base@sha256:0123", "mshkn-base"),
        ("python:3.12", "python"),
        ("ghcr.io/org/img:1", "ghcr.io/org/img"),
    ],
)
def test_image_name_strips_tag_and_digest(reference: str, name: str) -> None:
    assert image_name(reference) == name
