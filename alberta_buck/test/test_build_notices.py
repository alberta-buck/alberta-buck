"""Browser stubs and shared node_modules must not break build-time attribution."""
import importlib.util
import json
from pathlib import Path


def test_bundled_notices_skip_disabled_modules_and_resolve_used_packages(tmp_path):
    script                      = Path(__file__).resolve().parents[2] / "scripts/third_party_notices.py"
    spec                        = importlib.util.spec_from_file_location("notices", script)
    notices                     = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notices)
    package                     = tmp_path / "node_modules/example"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps(dict(name="example", version="1.0", license="MIT")))
    (package / "LICENSE").write_text("Example licence text")
    base                        = tmp_path / "worktree/core/js/artifacts"
    base.mkdir(parents=True)
    meta                        = base / "sandbox-meta.json"
    meta.write_text(json.dumps(dict(outputs={"app.js": dict(inputs={
        "../../../node_modules/example/index.js": dict(bytesInOutput=20),
        "(disabled):../../../node_modules/absent/index.js": dict(bytesInOutput=30),
        "../../../node_modules/unused/index.js": dict(bytesInOutput=0)})})))
    result                      = notices.bundled(meta)
    assert len(result) == 1
