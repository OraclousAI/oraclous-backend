import importlib

import pytest


@pytest.mark.unit
@pytest.mark.parametrize(
    "mod",
    [
        "oraclous_knowledge_graph_service.retrieval",
        "oraclous_knowledge_graph_service.retriever_service",
        "oraclous_knowledge_graph_service.retriever_factory",
        "oraclous_knowledge_graph_service.retriever_schemas",
    ],
)
def test_retrieval_shim_raises_import_error(mod: str) -> None:
    with pytest.raises(ImportError):
        importlib.import_module(mod)
