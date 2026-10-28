"""Write the API's OpenAPI document to openapi.json (run after changing routes or schemas)."""

import json
import pathlib

from app.main import app

path = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"
path.write_text(json.dumps(app.openapi(), indent=2) + "\n")
print(f"Wrote {path}")
