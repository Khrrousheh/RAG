import json
import unittest

import httpx

from backend.app.rag import (
    _fallback_answer,
    _llm_unavailable_warning,
    _model_name_candidates,
    _policy_note,
    _stream_event,
)
from backend.app.schemas import Source


def make_source(index: int, title: str = "Security Policy") -> Source:
    return Source(
        id=index,
        score=0.92,
        policy_name=title,
        file_name=f"{title}.pdf",
        page=2,
        department="IT",
        version="1.0",
        effective_date="2026-01-01",
        policy_title=title,
        text="Employees must protect confidential information and follow approval steps.",
    )


class RagHelperTests(unittest.TestCase):
    def test_policy_note_includes_policy_names_and_source_ids(self) -> None:
        note = _policy_note([make_source(1), make_source(2, "Data Protection Policy")])

        self.assertTrue(note.startswith("Policy note:"))
        self.assertIn("Security Policy [1]", note)
        self.assertIn("Data Protection Policy [2]", note)

    def test_llm_unavailable_warning_uses_safe_reason_categories(self) -> None:
        request = httpx.Request("POST", "http://model-runner.test/api/chat")
        timeout_warning = _llm_unavailable_warning(httpx.ReadTimeout("slow", request=request))
        response = httpx.Response(503, request=request)
        status_warning = _llm_unavailable_warning(
            httpx.HTTPStatusError("bad status", request=request, response=response)
        )
        empty_warning = _llm_unavailable_warning(RuntimeError("returned an empty answer"))

        self.assertEqual(
            timeout_warning,
            "LLM unavailable: request timed out. Showing retrieved policy context instead.",
        )
        self.assertEqual(
            status_warning,
            "LLM unavailable: service returned HTTP 503. Showing retrieved policy context instead.",
        )
        self.assertEqual(
            empty_warning,
            "LLM unavailable: empty response. Showing retrieved policy context instead.",
        )

    def test_fallback_answer_prefixes_llm_warning_only_for_llm_failures(self) -> None:
        warning = _llm_unavailable_warning(
            httpx.ConnectError(
                "connection refused",
                request=httpx.Request("POST", "http://model-runner.test/api/chat"),
            )
        )
        llm_fallback = _fallback_answer("What applies?", [make_source(1)], llm_warning=warning)
        deterministic_fallback = _fallback_answer("What applies?", [make_source(1)])

        self.assertTrue(llm_fallback.startswith("LLM unavailable: connection failed."))
        self.assertIn("Here are the strongest retrieved policy references:", llm_fallback)
        self.assertIn("[1] Security Policy", llm_fallback)
        self.assertFalse(deterministic_fallback.startswith("LLM unavailable:"))

    def test_stream_event_serializes_ndjson(self) -> None:
        line = _stream_event("warning", message="Policy note: Retrieved policy context from Security Policy [1].")

        self.assertTrue(line.endswith("\n"))
        payload = json.loads(line)
        self.assertEqual(payload["event"], "warning")
        self.assertEqual(
            payload["message"],
            "Policy note: Retrieved policy context from Security Policy [1].",
        )

    def test_model_name_candidates_include_hugging_face_variants(self) -> None:
        candidates = _model_name_candidates("hf.co/microsoft/Phi-3-mini-4k-instruct-gguf")

        self.assertIn("hf.co/microsoft/Phi-3-mini-4k-instruct-gguf", candidates)
        self.assertIn("microsoft/Phi-3-mini-4k-instruct-gguf", candidates)
        self.assertIn("Phi-3-mini-4k-instruct-gguf", candidates)
        self.assertIn("hf.co/microsoft/Phi-3-mini-4k-instruct-gguf:latest", candidates)
        self.assertNotIn("docker.io/hf.co/microsoft/Phi-3-mini-4k-instruct-gguf", candidates)


if __name__ == "__main__":
    unittest.main()
