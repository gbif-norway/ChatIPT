import re
from html import unescape
from pathlib import Path


TERM_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
EXAMPLE_PATTERN = re.compile(r"`([^`]+)`")
IGNORED_SECTIONS = {"Cite Darwin Core"}


def get_section_order(file_path):
    sections = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("## "):
            continue

        section_name = line[3:].strip()
        if section_name and section_name not in IGNORED_SECTIONS and section_name not in sections:
            sections.append(section_name)
    return sections


def next_non_empty_index(lines, start_index):
    index = start_index
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def is_term_header(lines, index):
    if index >= len(lines):
        return False
    candidate = lines[index].strip()
    if not TERM_NAME_PATTERN.fullmatch(candidate):
        return False
    next_index = next_non_empty_index(lines, index + 1)
    return next_index < len(lines) and lines[next_index].strip().startswith("Identifier")


def parse_examples(lines, start_index):
    examples = []
    line = lines[start_index].strip()
    inline_examples = EXAMPLE_PATTERN.findall(line[len("Examples"):].strip())
    examples.extend(item.strip() for item in inline_examples if item.strip())

    index = start_index + 1
    while index < len(lines):
        stripped = lines[index].strip()

        if stripped.startswith("## ") or is_term_header(lines, index):
            break
        if stripped.startswith("* ") or stripped.startswith("- "):
            values = EXAMPLE_PATTERN.findall(stripped)
            examples.extend(item.strip() for item in values if item.strip())
        index += 1

    return examples, index


def parse_term_block(lines, start_index):
    term_name = lines[start_index].strip()
    definition = ""
    examples = []

    index = start_index + 1
    while index < len(lines):
        stripped = lines[index].strip()

        if stripped.startswith("## ") or is_term_header(lines, index):
            break
        if stripped.startswith("Definition "):
            definition = stripped[len("Definition "):].strip()
        elif stripped.startswith("Examples"):
            parsed_examples, next_index = parse_examples(lines, index)
            examples.extend(parsed_examples)
            index = next_index
            continue
        index += 1

    return term_name, definition, examples, index


def parse_markdown_terms(file_path):
    lines = file_path.read_text(encoding="utf-8").splitlines()
    parsed_terms = {}
    section_order = get_section_order(file_path)
    section_set = set(section_order)

    section = None
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        section_match = re.match(r"^##\s+(.+)$", stripped)
        if section_match:
            heading = section_match.group(1).strip()
            section = heading if heading in section_set else None
            index += 1
            continue

        if section and is_term_header(lines, index):
            term_name, definition, examples, next_index = parse_term_block(lines, index)
            parsed_terms.setdefault(section, {})[term_name] = {
                "definition": definition,
                "examples": examples,
            }
            index = next_index
            continue

        index += 1

    return parsed_terms, section_order


def parse_html_terms(file_path):
    content = file_path.read_text(encoding="utf-8")
    parsed_terms = {}
    section_order = get_section_order(file_path)
    section_set = set(section_order)

    section_pattern = re.compile(r"^##\s+([^\n]+)\n(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
    table_pattern = re.compile(r'<table class="table">.*?<tbody>.*?</tbody>\s*</table>', re.DOTALL)

    for match in section_pattern.finditer(content):
        section_name = match.group(1).strip()
        if section_name not in section_set:
            continue

        section_content = match.group(2)
        for table in table_pattern.findall(section_content):
            term_name_match = re.search(r'<tr class="table-(?:secondary|primary)"><th colspan="2">(.*?)</th></tr>', table)
            if not term_name_match:
                continue

            term_name_raw = term_name_match.group(1).strip()
            term_name = unescape(re.sub(r"<.*?>", "", term_name_raw).strip())
            if term_name.endswith(" Class"):
                term_name = term_name[: -len(" Class")].strip()
            definition_match = re.search(r'<tr><td>Definition</td><td>(.*?)</td></tr>', table, re.DOTALL)
            examples_match = re.search(r'<tr><td>Examples</td><td>(.*?)</td></tr>', table, re.DOTALL)

            definition = ""
            if definition_match:
                definition = unescape(re.sub(r"<.*?>", "", definition_match.group(1)).strip())

            examples = []
            if examples_match:
                examples_cell = examples_match.group(1)
                code_examples = re.findall(r"<code>(.*?)</code>", examples_cell, re.DOTALL)
                examples = [unescape(value.strip()) for value in code_examples if value.strip()]
                if not examples:
                    plain_example = unescape(re.sub(r"<.*?>", "", examples_cell).strip())
                    if plain_example:
                        examples = [plain_example]

            parsed_terms.setdefault(section_name, {})[term_name] = {
                "definition": definition,
                "examples": examples,
            }

    return parsed_terms, section_order


def format_terms(parsed_terms, section_order):
    lines = []
    for section in section_order:
        section_terms = parsed_terms.get(section, {})
        if not section_terms:
            continue

        lines.append(f"{section}:")
        for term_name, details in section_terms.items():
            value = details["definition"]
            if details["examples"]:
                value += f" Egs - {' / '.join(details['examples'])}"
            escaped_value = value.replace("'", "''")
            lines.append(f"  {term_name}: '{escaped_value}'")

    return "\n".join(lines) + "\n"


def main():
    base_dir = Path(__file__).resolve().parent.parent
    input_file_path = base_dir / "templates" / "dwc-quick-reference-guide.md.txt"
    output_file_path = base_dir / "templates" / "dwc-quick-reference-guide.yaml"

    parsed_terms, section_order = parse_html_terms(input_file_path)
    if not parsed_terms:
        parsed_terms, section_order = parse_markdown_terms(input_file_path)
    output_file_path.write_text(format_terms(parsed_terms, section_order), encoding="utf-8")


if __name__ == "__main__":
    main()
