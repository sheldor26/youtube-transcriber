from __future__ import annotations

import csv
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

from app.models import Batch
from app.utils import destination_dir_for, sanitize_filename, unique_path, atomic_write_text

KNOWLEDGE_THEMES = {
    "backlinks": ["backlink", "link building", "anchor", "guest post", "niche edit", "link"],
    "authority": ["authority", "trust", "brand", "domain rating", "dr ", "da "],
    "content": ["content", "article", "page", "topic", "keyword", "copy"],
    "relevance": ["relevance", "relevant", "topical", "niche", "semantic"],
    "competition": ["competition", "competitor", "serp", "ranking", "rank"],
    "audit": ["audit", "crawl", "index", "technical", "schema", "internal link"],
    "local_seo": ["local seo", "maps", "gbp", "google business", "location"],
    "ai": ["ai", "chatgpt", "llm", "automation", "generated"],
    "pbn": ["pbn", "private blog", "network", "expired domain"],
    "scams": ["scam", "cheap", "fiverr", "lifetime", "guarantee", "package"],
    "penalties": ["penalty", "manual action", "spam", "deindex", "risk"],
    "metrics": ["ahrefs", "semrush", "majestic", "traffic", "metrics"],
}

EDITORIAL_THEMES = {
    "Performance and results": ["works", "result", "cooks", "heats", "power", "performance", "quality"],
    "Ease of use": ["easy", "simple", "clean", "cleaning", "use", "program", "button"],
    "Limitations and issues": ["problem", "failure", "noise", "difficult", "worse", "drawback", "disadvantage", "smell"],
    "Durability and materials": ["durable", "lasts", "material", "plastic", "metal", "broken", "resistant", "warranty"],
    "Purchasing and user profiles": ["worth", "price", "value", "family", "person", "space", "buy", "recommend"],
}

SUMMARY_STOPWORDS = {
    "a", "al", "con", "de", "del", "el", "en", "es", "la", "las", "lo", "los", "para", "por", "que", "se", "su", "un", "una", "y",
    "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the", "to", "with",
}

NEGATION_WORDS = {"no", "nunca", "jamas", "jamás", "sin", "ningun", "ninguna", "ninguno"}
NUMBER_WORDS = {
    "cero", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
}


def split_sentences(text: str) -> List[str]:
    compact = re.sub(r"\s+", " ", text).strip()
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", compact) if part.strip()]


def summary_tokens(text: str) -> List[str]:
    return [word for word in re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", text.lower()) if word not in SUMMARY_STOPWORDS]


def claim_markers(text: str) -> Dict[str, set[str]]:
    words = re.findall(r"[a-zA-ZÀ-ÿ0-9]+", text.lower())
    return {
        "negations": {word for word in words if word in NEGATION_WORDS},
        "numbers": {word for word in words if word.isdigit() or word in NUMBER_WORDS},
    }


def sentences_are_duplicate(first: str, second: str) -> bool:
    first_markers = claim_markers(first)
    second_markers = claim_markers(second)
    if bool(first_markers["negations"]) != bool(second_markers["negations"]):
        return False
    if first_markers["numbers"] and second_markers["numbers"] and first_markers["numbers"] != second_markers["numbers"]:
        return False
    first_tokens = set(summary_tokens(first))
    second_tokens = set(summary_tokens(second))
    if not first_tokens or not second_tokens:
        return False
    overlap = len(first_tokens & second_tokens) / max(1, len(first_tokens | second_tokens))
    return overlap >= 0.78 and SequenceMatcher(None, first.lower(), second.lower()).ratio() >= 0.78


def build_consolidated_summary(batch: Batch) -> Optional[Path]:
    completed_results = [
        result
        for result in batch.results
        if result.get("status") in {"done", "skipped"} and result.get("transcript_path") and Path(result["transcript_path"]).exists()
    ]
    if not completed_results:
        return None

    all_sentences = []
    document_frequency = Counter()
    for order, result in enumerate(completed_results, start=1):
        path = Path(result["transcript_path"])
        text = path.read_text(encoding="utf-8", errors="ignore")
        sentences = split_sentences(text)
        for position, sentence in enumerate(sentences):
            tokens = summary_tokens(sentence)
            if len(tokens) < 5:
                continue
            document_frequency.update(set(tokens))
            all_sentences.append({
                "order": order,
                "position": position,
                "title": result.get("title") or path.stem,
                "url": result.get("url") or "",
                "sentence": sentence,
                "tokens": tokens,
            })

    if not all_sentences:
        return None

    ranked = sorted(
        all_sentences,
        key=lambda item: (
            sum(document_frequency[token] for token in set(item["tokens"])) / len(set(item["tokens"])),
            min(len(item["tokens"]), 45),
        ),
        reverse=True,
    )
    selected = []
    selected_words = 0
    max_words = max(500, batch.summary_max_words)
    for candidate in ranked:
        if any(sentences_are_duplicate(candidate["sentence"], item["sentence"]) for item in selected):
            continue
        candidate_words = len(candidate["tokens"])
        if selected and selected_words + candidate_words > max_words:
            continue
        selected.append(candidate)
        selected_words += candidate_words
        if selected_words >= max_words:
            break

    total_sources = len(completed_results)
    for item in selected:
        item["source_count"] = len({
            source["order"]
            for source in all_sentences
            if sentences_are_duplicate(item["sentence"], source["sentence"])
        })
    selected.sort(key=lambda item: (item["order"], item["position"]))
    lines = [
        "CONSOLIDATED SUMMARY",
        "",
        f"Videos analyzed: {len(completed_results)}",
        "This summary was generated locally by selecting frequent sentences and removing exact or near-textual repetitions.",
        "The count indicates textual matches, not semantic agreement or factual accuracy. Review the original transcripts before using a conclusion.",
        "Each point retains its source so it can be checked against the original transcript.",
        "",
    ]
    current_order = None
    for item in selected:
        if item["order"] != current_order:
            current_order = item["order"]
            lines.extend([f"## {item['title']}", ""])
            if item["url"]:
                lines.append(f"Source: {item['url']}")
                lines.append("")
        lines.append(f"- [Textual match: {item['source_count']} of {total_sources} videos] {item['sentence']}")
    lines.extend(["", f"Selected words: {selected_words}", ""])

    destination_dir = destination_dir_for(batch.output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    summary_path = unique_path(destination_dir / f"consolidated-summary-{batch.id}.txt")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def editorial_theme(sentence: str) -> str:
    lowered = sentence.lower()
    scores = {theme: sum(keyword in lowered for keyword in keywords) for theme, keywords in EDITORIAL_THEMES.items()}
    return max(scores, key=scores.get) if max(scores.values(), default=0) else "General observations"


def build_editorial_material(
    product_name: str,
    manufacturer_text: str,
    transcriptions_dir: str,
    max_words: int,
    transcript_files: Optional[List[Path]] = None,
) -> Path:
    directory = destination_dir_for(transcriptions_dir)
    if transcript_files is None and (not directory.exists() or not directory.is_dir()):
        raise RuntimeError("The transcript folder does not exist.")

    if transcript_files is None:
        transcript_files = sorted(
            path for path in directory.glob("*.txt")
            if not path.name.startswith(("editorial-material-", "consolidated-summary-"))
        )
    else:
        transcript_files = sorted(
            {path.resolve() for path in transcript_files if path.exists() and path.is_file()},
            key=lambda path: path.name.casefold(),
        )
    if not transcript_files:
        raise RuntimeError("No usable transcripts were found for this research project.")

    manufacturer_sentences = split_sentences(manufacturer_text)
    candidates = []
    frequency = Counter()
    for source_index, path in enumerate(transcript_files, start=1):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for position, sentence in enumerate(split_sentences(text)):
            tokens = summary_tokens(sentence)
            if len(tokens) < 5:
                continue
            if any(sentences_are_duplicate(sentence, known) for known in manufacturer_sentences):
                continue
            frequency.update(set(tokens))
            candidates.append({"sentence": sentence, "tokens": tokens, "theme": editorial_theme(sentence), "position": position, "source_index": source_index})

    ranked = sorted(
        candidates,
        key=lambda item: sum(frequency[token] for token in set(item["tokens"])) / len(set(item["tokens"])),
        reverse=True,
    )
    selected = []
    selected_words = 0
    for candidate in ranked:
        if any(sentences_are_duplicate(candidate["sentence"], item["sentence"]) for item in selected):
            continue
        words = len(candidate["tokens"])
        if selected and selected_words + words > max_words:
            continue
        selected.append(candidate)
        selected_words += words
        if selected_words >= max_words:
            break

    total_sources = len(transcript_files)
    for item in selected:
        item["source_count"] = len({
            source["source_index"]
            for source in candidates
            if sentences_are_duplicate(item["sentence"], source["sentence"])
        })
    grouped: Dict[str, List[str]] = {}
    for item in selected:
        grouped.setdefault(item["theme"], []).append(item["sentence"])
    lines = [
        "EDITORIAL RESEARCH MATERIAL",
        "",
        f"Product or topic: {product_name or 'Not specified'}",
        f"Files analyzed: {len(transcript_files)}",
        "",
        "This document groups practical experience statements found in the selected transcripts.",
        "Statements similar to supplied manufacturer information were excluded.",
        "The count measures textual matches, not consensus or factual accuracy. This is not publication-ready copy and should be reviewed before editorial use.",
        "",
        "## POTENTIALLY NEW INFORMATION",
        "",
    ]
    for theme, sentences in grouped.items():
        lines.extend([f"### {theme}", ""])
        lines.extend(f"- [Textual match: {item['source_count']} of {total_sources} videos] {item['sentence']}" for item in selected if item["theme"] == theme)
        lines.append("")
    lines.extend([
        "## AI INSTRUCTIONS",
        "",
        "Use this material to improve a buying guide. Do not mention reviewers or copy exact phrases.",
        "Form an original conclusion from repeated patterns, distinguish experience from specifications, and do not invent facts.",
        "",
    ])

    filename = sanitize_filename(f"editorial-material-{product_name or 'product'}")
    directory.mkdir(parents=True, exist_ok=True)
    output_path = unique_path(directory / f"{filename}.txt")
    atomic_write_text(output_path, "\n".join(lines))
    return output_path


def matching_theme(sentence: str) -> Optional[str]:
    lowered = sentence.lower()
    best_theme = None
    best_score = 0
    for theme, keywords in KNOWLEDGE_THEMES.items():
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score > best_score:
            best_score = score
            best_theme = theme
    return best_theme


def build_knowledge_base(batch: Batch) -> Optional[Path]:
    completed_results = [
        result
        for result in batch.results
        if result.get("status") in {"done", "skipped"} and result.get("transcript_path") and Path(result["transcript_path"]).exists()
    ]
    if not completed_results:
        return None

    destination_dir = destination_dir_for(batch.output_dir)
    kb_dir = destination_dir / f"knowledge-base-{batch.id}"
    kb_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = kb_dir / "evidence-by-theme"
    evidence_dir.mkdir(exist_ok=True)

    theme_hits: Dict[str, List[Dict[str, str]]] = {theme: [] for theme in KNOWLEDGE_THEMES}
    index_rows = []
    for order, result in enumerate(completed_results, start=1):
        path = Path(result["transcript_path"])
        text = path.read_text(encoding="utf-8", errors="ignore")
        words = len(re.findall(r"\w+", text))
        index_rows.append(
            {
                "order": order,
                "title": result.get("title") or path.stem,
                "url": result.get("url") or "",
                "status": result.get("status") or "",
                "source": result.get("source") or "",
                "transcript_path": str(path),
                "words": words,
            }
        )

        for sentence in split_sentences(text):
            theme = matching_theme(sentence)
            if theme and len(theme_hits[theme]) < 80:
                theme_hits[theme].append(
                    {
                        "title": result.get("title") or path.stem,
                        "url": result.get("url") or "",
                        "sentence": sentence[:600],
                    }
                )

    index_fields = ["order", "title", "url", "status", "source", "transcript_path", "words"]
    with (kb_dir / "index.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=index_fields)
        writer.writeheader()
        writer.writerows(index_rows)

    theme_rows = []
    for theme, hits in sorted(theme_hits.items(), key=lambda item: len(item[1]), reverse=True):
        theme_rows.append({"theme": theme, "mentions": len(hits)})
        if not hits:
            continue
        lines = [f"# {theme}", ""]
        for hit in hits[:40]:
            lines.append(f"- {hit['sentence']}")
            if hit["url"]:
                lines.append(f"  Source: {hit['title']} - {hit['url']}")
        (evidence_dir / f"{theme}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (kb_dir / "themes.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["theme", "mentions"])
        writer.writeheader()
        writer.writerows(theme_rows)

    top_themes = [row for row in theme_rows if row["mentions"] > 0][:8]
    readme = [
        "# Knowledge base",
        "",
        f"Batch: {batch.id}",
        f"Transcripts analyzed: {len(completed_results)}",
        "",
        "## Files",
        "",
        "- `index.csv`: inventory of the transcripts used.",
        "- `themes.csv`: detected-theme counts.",
        "- `evidence-by-theme/`: relevant sentences grouped by theme.",
        "- `action-ideas.md`: starting checklist for turning the material into SEO improvements.",
        "",
        "## Top themes",
        "",
    ]
    readme.extend([f"- {row['theme']}: {row['mentions']} mentions" for row in top_themes] or ["- No themes had enough evidence."])
    (kb_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")

    ideas = [
        "# Action ideas",
        "",
        "1. Review the themes with the most evidence in `themes.csv` and open the files in `evidence-by-theme/`.",
        "2. Turn each important statement into an operational rule: what to inspect, how to measure it, and what action to take.",
        "3. Separate actions by impact: content, links, technical audit, authority, competition, and risk.",
        "4. Use the source material to ask an AI for a deeper playbook without losing traceability to each video.",
        "",
        "Suggested prompt:",
        "",
        "Study this SEO transcript knowledge base. Extract principles, warnings, repeated tactics, contradictions, and opportunities to improve my tools and sites. Cite the source file or video when you propose an action.",
    ]
    (kb_dir / "action-ideas.md").write_text("\n".join(ideas) + "\n", encoding="utf-8")

    return kb_dir
