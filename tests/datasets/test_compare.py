from pathlib import Path

from lerobot.data_platform.precompute.compare.overlap import scenario_distribution, vocab_venn


class DummyMeta:
    def __init__(self, root: Path, tasks: dict[int, str]):
        self.root = root
        self.tasks = tasks


def _write_tasks(root: Path, tasks: dict[int, str]) -> None:
    meta = root / "meta"
    meta.mkdir(parents=True)
    with (meta / "tasks.jsonl").open("w") as f:
        for task_index, task in tasks.items():
            f.write(f'{{"task_index": {task_index}, "task": "{task}"}}\n')


def test_compare_vocab_and_scenarios(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    tasks_a = {0: "Pick up the yellow duck", 1: "Give me the brown dog"}
    tasks_b = {0: "Pick up the yellow duck on the left", 1: "Pick up the green dinosaur"}
    _write_tasks(a, tasks_a)
    _write_tasks(b, tasks_b)

    venn = vocab_venn(DummyMeta(a, tasks_a), DummyMeta(b, tasks_b))
    assert venn["both"] == ["yellow duck"]
    assert "brown dog" in venn["a_only"]
    assert "green dinosaur" in venn["b_only"]
    assert scenario_distribution(DummyMeta(a, tasks_a)) == {"give": 1, "single_pick": 1}
