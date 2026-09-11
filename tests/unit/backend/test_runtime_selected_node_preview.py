from cv_contracts.runtime import RuntimePipelineGraph, RuntimePipelinePreviewRequest

from runtime_admin_api.api.routes import _preview_graph_through_selected_node


def test_selected_node_preview_executes_only_the_node_and_its_ancestors() -> None:
    graph = RuntimePipelineGraph.model_validate(
        {
            "nodes": [
                {
                    "node_key": "frame_source",
                    "node_type": "source.channel_frame",
                    "node_category": "source",
                },
                {
                    "node_key": "board_end",
                    "node_type": "analysis.board_end_coordinate_frame",
                    "node_category": "analysis",
                },
                {
                    "node_key": "edge_branch",
                    "node_type": "computer_vision.canny_edge_detection",
                    "node_category": "computer_vision",
                },
                {
                    "node_key": "output",
                    "node_type": "output.result",
                    "node_category": "output",
                },
            ],
            "edges": [
                {
                    "from_node_key": "frame_source",
                    "from_port": "frame",
                    "to_node_key": "board_end",
                    "to_port": "frame",
                },
                {
                    "from_node_key": "frame_source",
                    "from_port": "frame",
                    "to_node_key": "edge_branch",
                    "to_port": "frame",
                },
                {
                    "from_node_key": "board_end",
                    "from_port": "coordinate_frame",
                    "to_node_key": "output",
                    "to_port": "result",
                },
            ],
        }
    )

    preview_graph = _preview_graph_through_selected_node(graph, "board_end")

    assert [node.node_key for node in preview_graph.nodes] == [
        "frame_source",
        "board_end",
    ]
    assert len(preview_graph.edges) == 1


def test_pipeline_preview_accepts_an_exact_frozen_input_frame() -> None:
    request = RuntimePipelinePreviewRequest.model_validate(
        {
            "authoring_graph": {"nodes": [], "edges": []},
            "preview_frame": {
                "image_base64": "aW1hZ2U=",
                "mime_type": "image/png",
                "width": 320,
                "height": 240,
                "sequence": 17,
                "metadata": {"frozen_for_tuning": True},
            },
        }
    )

    assert request.preview_frame is not None
    assert request.preview_frame.sequence == 17
    assert request.preview_frame.image_base64 == "aW1hZ2U="
