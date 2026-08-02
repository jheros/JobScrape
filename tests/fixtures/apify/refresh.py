"""Re-vendor the Apify actors' published dataset schemas.

    python tests/fixtures/apify/refresh.py

Run this when an actor ships a new build and `test_apify.py`'s conformance
tests start failing, or before adding a field to a mapper. The endpoint is
public — a token is only needed to read *datasets*, not an actor's definition.

Why these are vendored rather than fetched in the tests: a test suite that
reaches the network fails for reasons that have nothing to do with the code, and
these schemas change a few times a year. Vendoring makes the version under test
explicit and reviewable in a diff.

The mappers were originally written from a prose summary of the actors' store
pages rather than from these files. That produced two blocking bugs and a
27-field gap, which is the whole reason the conformance tests exist.
"""

from __future__ import annotations

import json
import pathlib
import urllib.request

#: Actor slug -> opaque actor id. The id is what a run reports as `actId`, so it
#: is also what `sources/apify` has to resolve a mapper from.
ACTORS = {
    "indeed": "nlZZi3lZre4fM9IET",
    "linkedin": "2rJKkhh7vjpX7pvjg",
}

HERE = pathlib.Path(__file__).parent


def main() -> None:
    for name, actor_id in ACTORS.items():
        url = f"https://api.apify.com/v2/acts/{actor_id}/builds/default"
        with urllib.request.urlopen(url, timeout=40) as response:
            build = json.load(response)["data"]
        definition = build["actorDefinition"]
        document = {
            "_source": url,
            "_actor": definition.get("name"),
            "_actor_id": actor_id,
            "_build_number": build.get("buildNumber"),
            "_fetched_note": (
                "Vendored from the endpoint above, which is public and needs no "
                "token. Refresh with tests/fixtures/apify/refresh.py when an "
                "actor changes."
            ),
            "dataset": definition["storages"]["dataset"],
        }
        path = HERE / f"{name}-dataset-schema.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        fields = document["dataset"]["fields"]["properties"]
        print(f"{path.name}: {len(fields)} fields (build {build.get('buildNumber')})")


if __name__ == "__main__":
    main()
