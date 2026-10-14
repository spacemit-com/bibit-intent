"""Select paired external test utterances without consulting classifier outputs."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import unicodedata


ARCHIVE_SHA256 = "7df623fd2d300a4d235d6ee5bd396c9a28258d3a0ccb29abdb054506eba153f8"


def normalize(text):
    return "".join(character for character in unicodedata.normalize("NFKC", text).casefold() if character.isalnum())


def select(archive_path, repository, count):
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if digest != ARCHIVE_SHA256:
        raise ValueError("MASSIVE 1.0 archive SHA-256 mismatch")
    known = set()
    for root in (repository / "tests", repository / "src/bibit_intent/resources"):
        for path in root.rglob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                row = json.loads(line)
                for key in ("text", "utt", "input", "query", "utterance"):
                    if isinstance(row.get(key), str):
                        known.add(normalize(row[key]))
    with tarfile.open(archive_path) as archive:
        languages = {}
        for locale in ("en-US", "zh-CN"):
            with archive.extractfile(f"1.0/data/{locale}.jsonl") as stream:
                languages[locale] = {row["id"]: row for row in map(json.loads, stream) if row["partition"] == "test"}
    common = set.intersection(*(set(rows) for rows in languages.values()))
    ordered = sorted(common, key=lambda identity: hashlib.sha256(f"bibit-holdout-v1:{identity}".encode()).hexdigest())
    result = []
    for identity in ordered:
        pair = [languages[locale][identity] for locale in ("en-US", "zh-CN")]
        if any(normalize(row["utt"]) in known for row in pair):
            continue
        for row in pair:
            known.add(normalize(row["utt"]))
            result.append({"id": f"massive-1.0:{row['locale']}:{identity}", "text": row["utt"],
                           "language": row["locale"], "source_intent": row["intent"], "source_partition": "test",
                           "label": None, "annotation_status": "unreviewed"})
        if len(result) == count * 2:
            break
    if len(result) != count * 2:
        raise ValueError("not enough independent paired samples")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pairs", type=int, default=50)
    parser.add_argument("--labels", type=Path)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    rows = select(args.archive, Path(__file__).resolve().parents[1], args.pairs)
    if args.labels:
        labels = json.loads(args.labels.read_text(encoding="utf-8"))
        if args.pairs != 50:
            parser.error("frozen labels only apply to the 50-pair v1 sample")
        if {row["id"].rsplit(":", 1)[1] for row in rows} != set(labels["sample_pair_ids"]):
            parser.error("sample changed; use a new annotation manifest rather than reusing labels")
        for row in rows:
            identity = row["id"].rsplit(":", 1)[1]
            row["label"] = "complex" if identity in labels["complex_pair_ids"] else labels["default_label"]
            row["annotation_status"] = labels["annotation_status"]
            row["policy_version"] = labels["policy_version"]
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


if __name__ == "__main__":
    main()
