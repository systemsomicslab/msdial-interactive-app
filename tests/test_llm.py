import unittest

from msdial_app.llm import resolve_llm_config


class LlmConfigTests(unittest.TestCase):
    def test_local_openai_compatible_accepts_loopback_without_key(self):
        resolved = resolve_llm_config(
            {
                "provider": "local-openai-compatible",
                "endpoint": "http://127.0.0.1:11434/v1",
                "deployment": "qwen3:8b",
                "api_key": "",
            }
        )

        self.assertIsNotNone(resolved)
        self.assertEqual("local-openai-compatible", resolved["provider"])
        self.assertEqual("", resolved["api_key"])

    def test_local_openai_compatible_rejects_non_loopback_endpoint(self):
        resolved = resolve_llm_config(
            {
                "provider": "local-openai-compatible",
                "endpoint": "https://models.example.org/v1",
                "deployment": "example-model",
                "api_key": "",
            }
        )

        self.assertIsNone(resolved)

    def test_cloud_openai_compatible_still_requires_key(self):
        resolved = resolve_llm_config(
            {
                "provider": "openai-compatible",
                "endpoint": "https://models.example.org/v1",
                "deployment": "example-model",
                "api_key": "",
            }
        )

        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
