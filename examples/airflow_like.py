from agentflow import Graph, codex


with Graph("airflow-like-example", working_dir=".", concurrency=3) as dag:
    plan = codex(
        task_id="plan",
        prompt="Inspect the repo and produce a concise plan.",
        tools="read_only",
    )
    implement = codex(
        task_id="implement",
        prompt="Implement the approved plan:\n\n{{ nodes.plan.output }}",
        tools="read_write",
    )
    review = codex(
        task_id="review",
        prompt="Review the plan and call out risks:\n\n{{ nodes.plan.output }}",
        capture="trace",
    )
    merge = codex(
        task_id="merge",
        prompt=(
            "Merge the implementation and review into one final response.\n\n"
            "Implementation:\n{{ nodes.implement.output }}\n\n"
            "Review:\n{{ nodes.review.output }}"
        ),
    )

    plan >> [implement, review]
    [implement, review] >> merge

print(dag.to_json())
