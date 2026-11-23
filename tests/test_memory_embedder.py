from unittest import TestCase

from src.adapters.models.litellm_model import create_memory_embedder


class MemoryEmbedderFactoryTests(TestCase):
    def test_factory_keeps_configured_model_name_without_loading_model(self):
        embedder = create_memory_embedder({"memory_embeddings": {"model": "custom/model"}})

        self.assertEqual(embedder.model_name, "fastembed:custom/model")

    def test_false_disables_memory_embeddings(self):
        self.assertIsNone(create_memory_embedder({"memory_embeddings": False}))
