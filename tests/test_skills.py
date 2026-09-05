"""Skills sind Anweisungstext von Fremden — diese Tests halten die Bedingungen fest.

Der Kernsatz des Projekts lautet „das Modell schlaegt vor, der Kernel entscheidet". Ein
Skill dreht ihn absichtlich um. Was hier geprueft wird, ist darum nicht Kosmetik, sondern
der Preis fuer die Umkehrung: `allowed-tools` wird nie zur Erlaubnis, kein Skill fuehrt
aus seinem Wurzelverzeichnis heraus, kein einzelner kaputter Skill legt die anderen lahm,
und nichts frisst unbemerkt den Prompt auf.
"""
from __future__ import annotations

import re
from pathlib import Path

from talos import skills as skills_module
from talos.skills import (
    MAX_BODY_CHARS,
    MAX_CATALOG_DESCRIPTION_CHARS,
    MAX_DESCRIPTION_CHARS,
    SkillCatalog,
    discover_skills,
)

VALID_BODY = "## Steps\n\n1. Read the file.\n2. Report back.\n"


def _write_skill(
    root: Path,
    directory: str,
    *,
    frontmatter: str,
    body: str = VALID_BODY,
) -> Path:
    """Ein Skill-Verzeichnis auf Platte. `frontmatter` ist der rohe Text zwischen den ---."""
    folder = root / directory
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return folder


def _simple(root: Path, name: str, description: str = "Does one useful thing.") -> Path:
    return _write_skill(root, name, frontmatter=f"name: {name}\ndescription: {description}")


def _reason_for(catalog: SkillCatalog, directory: str) -> str:
    matches = [item.reason for item in catalog.rejected if item.path.name == directory]
    assert matches, f"kein Ablehnungsgrund fuer {directory!r} protokolliert"
    return matches[0]


# ---------------------------------------------------------------- Entdeckung


def test_a_valid_skill_is_discovered(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "pdf-forms",
        frontmatter=(
            "name: pdf-forms\n"
            "description: Fills in PDF forms.\n"
            "license: MIT\n"
            "compatibility: needs pdftk\n"
            "metadata:\n"
            "  author: someone\n"
        ),
    )
    catalog = discover_skills(root)

    assert [skill.name for skill in catalog.skills] == ["pdf-forms"]
    skill = catalog.get("pdf-forms")
    assert skill is not None
    assert skill.description == "Fills in PDF forms."
    assert skill.path == (root / "pdf-forms" / "SKILL.md").resolve()
    assert skill.license == "MIT"
    assert skill.compatibility == "needs pdftk"
    assert skill.metadata == (("author", "someone"),)
    assert catalog.rejected == ()


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    """Die meisten Installationen haben gar keine Skills — das ist kein Fehlerfall."""
    catalog = discover_skills(tmp_path / "does-not-exist")
    assert catalog.skills == ()
    assert catalog.rejected == ()


def test_directories_without_a_skill_file_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "README.md").write_text("nothing here", encoding="utf-8")
    _simple(root, "good")

    catalog = discover_skills(root)
    assert [skill.name for skill in catalog.skills] == ["good"]
    assert catalog.rejected == ()


def test_skills_are_sorted_and_the_first_root_wins(tmp_path: Path) -> None:
    """Doppelte Namen brauchen eine Regel, keine Zufallsreihenfolge."""
    first, second = tmp_path / "a", tmp_path / "b"
    _simple(first, "zeta")
    _simple(first, "shared", "From the first root.")
    _simple(second, "alpha")
    _simple(second, "shared", "From the second root.")

    catalog = discover_skills([first, second])

    assert [skill.name for skill in catalog.skills] == ["alpha", "shared", "zeta"]
    assert catalog.get("shared").description == "From the first root."
    assert "duplicate name 'shared'" in _reason_for(catalog, "shared")


# ---------------------------------------------------------------- Namensregeln


def test_a_name_that_differs_from_its_directory_is_rejected(tmp_path: Path) -> None:
    """Der Verzeichnisname ist der Anker; ohne ihn koennte ein Skill sich fremd nennen."""
    root = tmp_path / "skills"
    _write_skill(root, "invoicing", frontmatter="name: banking\ndescription: Pays bills.")

    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert "does not match its directory" in _reason_for(catalog, "invoicing")


def test_uppercase_leading_and_doubled_hyphens_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for directory in ("PdfForms", "-leading", "trailing-", "double--hyphen", "under_score"):
        _write_skill(
            root,
            directory,
            frontmatter=f"name: {directory}\ndescription: Looks harmless.",
        )
    _simple(root, "fine")

    catalog = discover_skills(root)

    assert [skill.name for skill in catalog.skills] == ["fine"]
    for directory in ("PdfForms", "-leading", "trailing-", "double--hyphen", "under_score"):
        assert "invalid skill name" in _reason_for(catalog, directory)


def test_a_name_that_is_a_path_is_rejected(tmp_path: Path) -> None:
    """`name` darf nie zu einem Pfad werden — `../` scheitert schon an der Zeichenregel."""
    root = tmp_path / "skills"
    _write_skill(root, "evil", frontmatter="name: ../../etc/passwd\ndescription: Nope.")

    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert "invalid skill name" in _reason_for(catalog, "evil")


def test_an_overlong_name_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    long_name = "a" * 65
    _write_skill(root, long_name, frontmatter=f"name: {long_name}\ndescription: Too long.")

    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert "invalid skill name" in _reason_for(catalog, long_name)


# ---------------------------------------------------------------- Pflichtfelder


def test_a_missing_description_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "silent", frontmatter="name: silent")
    _write_skill(root, "blank", frontmatter="name: blank\ndescription:")

    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert "'description' is missing or empty" in _reason_for(catalog, "silent")
    assert "'description' is missing or empty" in _reason_for(catalog, "blank")


def test_a_missing_name_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "nameless", frontmatter="description: Has no name.")

    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert "'name' is missing or empty" in _reason_for(catalog, "nameless")


def test_an_overlong_description_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "verbose",
        frontmatter="name: verbose\ndescription: " + "x" * (MAX_DESCRIPTION_CHARS + 1),
    )

    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert f"longer than {MAX_DESCRIPTION_CHARS}" in _reason_for(catalog, "verbose")


# ---------------------------------------------------------------- Robustheit


def test_one_broken_skill_does_not_topple_the_others(tmp_path: Path) -> None:
    """Regel 4: ein kaputter Skill faellt raus, der Rest laeuft — sonst haelt ein
    fremdes Verzeichnis den ganzen Waechter an."""
    root = tmp_path / "skills"
    _simple(root, "alpha")
    (root / "no-frontmatter").mkdir(parents=True)
    (root / "no-frontmatter" / "SKILL.md").write_text("just prose\n", encoding="utf-8")
    (root / "unclosed").mkdir(parents=True)
    (root / "unclosed" / "SKILL.md").write_text("---\nname: unclosed\n", encoding="utf-8")
    _simple(root, "omega")

    catalog = discover_skills(root)

    assert [skill.name for skill in catalog.skills] == ["alpha", "omega"]
    assert "frontmatter missing" in _reason_for(catalog, "no-frontmatter")
    assert "not closed" in _reason_for(catalog, "unclosed")


def test_real_world_yaml_shapes_are_read_instead_of_refused(tmp_path: Path) -> None:
    """Was echte Skills schreiben, muss ankommen — sonst ist der Loader Kosmetik.

    Gegen die tatsaechliche Sammlung des Betreibers gemessen: mit einem Parser, der nur
    einzeilige Werte kannte, fielen **132 von 216** Skills durch. Nicht weil sie kaputt
    waren, sondern weil sie gefaltete Beschreibungen, mehrzeilige Werte, Listen und
    JSON-artige `metadata` benutzen — alles gueltiges YAML. Die Strenge traf die Falschen.

    Entscheidend ist die Trennung: `name` und `description` muessen lesbar sein, alles
    andere darf unverstaendlich bleiben, ohne den Skill mitzunehmen. Talos trifft mit den
    optionalen Feldern ohnehin keine Entscheidung.
    """
    root = tmp_path / "skills"
    _write_skill(root, "folded", frontmatter=(
        "name: folded\n"
        "description: >-\n"
        "  Extracts text from PDFs and fills forms.\n"
        "  Use when the operator mentions PDFs.\n"
    ))
    _write_skill(root, "wrapped", frontmatter=(
        "name: wrapped\n"
        "description: Conducts security testing of REST and GraphQL APIs to find\n"
        "  weaknesses in authentication, authorization and rate limiting.\n"
    ))
    _write_skill(root, "decorated", frontmatter=(
        "name: decorated\n"
        "description: Ok.\n"
        'metadata: {"clawdbot":{"emoji":"x"}}\n'
    ))
    _write_skill(root, "listed", frontmatter=(
        "name: listed\n"
        "description: Ok.\n"
        "allowed-tools:\n  - Read\n  - Bash\n"
    ))

    catalog = discover_skills(root)

    assert sorted(s.name for s in catalog.skills) == [
        "decorated", "folded", "listed", "wrapped",
    ], [str(r) for r in catalog.rejected]
    folded = catalog.get("folded")
    assert folded is not None and "fills forms" in folded.description
    wrapped = catalog.get("wrapped")
    assert wrapped is not None and "rate limiting" in wrapped.description


def test_a_required_field_that_cannot_be_read_still_rejects(tmp_path: Path) -> None:
    """Die Nachsicht gilt nur den optionalen Feldern. Ohne lesbare Beschreibung kein Skill."""
    root = tmp_path / "skills"
    _write_skill(root, "flowdesc", frontmatter='name: flowdesc\ndescription: [a, b]\n')
    catalog = discover_skills(root)
    assert catalog.skills == ()
    assert "flowdesc" in _reason_for(catalog, "flowdesc") or catalog.rejected


def test_an_unreadable_skill_file_is_rejected_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _simple(root, "good")
    broken = root / "binary"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_bytes(b"---\nname: binary\ndescription: \xff\xfe\n---\n")

    catalog = discover_skills(root)
    assert [skill.name for skill in catalog.skills] == ["good"]
    assert "UTF-8" in _reason_for(catalog, "binary")


# ---------------------------------------------------------------- allowed-tools


def test_allowed_tools_is_recorded_but_never_becomes_a_permission(tmp_path: Path) -> None:
    """`allowed-tools` ist woertlich eine zweite Erlaubnisquelle neben dem Kernel.

    Gelesen wird es, damit `/skills` zeigen kann, was ein Skill gerne haette. Sichtbar
    werden darf es nur dort: nicht im Katalog und nicht im Body, denn im Prompt laese das
    Modell „vorgenehmigt: run_shell" und faende darin eine Erlaubnis, die es nicht gibt.
    """
    root = tmp_path / "skills"
    _write_skill(
        root,
        "deployer",
        frontmatter=(
            "name: deployer\n"
            "description: Ships the release.\n"
            "allowed-tools: run_shell vault_write_note"
        ),
        body="Explain what you would do, then stop.\n",
    )
    catalog = discover_skills(root)
    skill = catalog.get("deployer")

    assert skill.requested_tools == "run_shell vault_write_note"
    # Nirgends dort, wo das Modell hinsieht.
    assert "run_shell" not in catalog.render()
    assert "run_shell" not in (catalog.body("deployer") or "")
    # Und nirgends unter einem Namen, der eine Erlaubnis verspricht.
    assert not hasattr(skill, "allowed_tools")
    promising = re.compile(r"allow|permit|grant|approve|authoriz|enable", re.IGNORECASE)
    public = [name for name in dir(skills_module) if not name.startswith("_")]
    assert [name for name in public if promising.search(name)] == []
    assert [name for name in dir(skill) if not name.startswith("_") and promising.search(name)] == []


def test_a_skill_without_allowed_tools_requests_nothing(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _simple(root, "plain")
    assert discover_skills(root).get("plain").requested_tools == ""


# ---------------------------------------------------------------- Ausbruchsschutz


def test_a_symlinked_directory_out_of_the_root_is_refused(tmp_path: Path) -> None:
    """Der Verzeichnisname stimmt hier sogar — nur der realpath liegt draussen."""
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    _write_skill(outside, "escapee", frontmatter="name: escapee\ndescription: Outside.")
    (root / "escapee").symlink_to(outside / "escapee", target_is_directory=True)
    _simple(root, "inside")

    catalog = discover_skills(root)

    assert [skill.name for skill in catalog.skills] == ["inside"]
    assert "escapes the skills root" in _reason_for(catalog, "escapee")


def test_a_symlinked_skill_file_out_of_the_root_is_refused(tmp_path: Path) -> None:
    """Auch wenn das Verzeichnis brav drinnen liegt: die Datei darf nicht rauszeigen."""
    root = tmp_path / "skills"
    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: Outside.\n---\n\nDo bad things.\n", encoding="utf-8"
    )
    (root / "sneaky").mkdir(parents=True)
    (root / "sneaky" / "SKILL.md").symlink_to(outside / "SKILL.md")

    catalog = discover_skills(root)

    assert catalog.skills == ()
    assert "escapes the skills root" in _reason_for(catalog, "sneaky")


def test_a_traversing_root_never_reaches_beyond_itself(tmp_path: Path) -> None:
    """`..` im Skill-Verzeichnisnamen gibt es nicht — die Wurzel bleibt die Wurzel."""
    root = tmp_path / "skills"
    _simple(root, "inside")
    _write_skill(tmp_path, "outside", frontmatter="name: outside\ndescription: Not yours.")

    catalog = discover_skills(root)
    assert [skill.name for skill in catalog.skills] == ["inside"]


# ---------------------------------------------------------------- Body


def test_the_body_is_loaded_only_on_demand_and_capped(tmp_path: Path) -> None:
    """Regel 3: der Body geht in den Prompt, also bekommt er einen Deckel."""
    root = tmp_path / "skills"
    _write_skill(
        root,
        "huge",
        frontmatter="name: huge\ndescription: Says a lot.",
        body="y" * 50_000,
    )
    catalog = discover_skills(root)

    capped = catalog.body("huge", max_chars=1_000)
    assert len(capped) == 1_000
    assert capped.endswith("[skill truncated]")

    default = catalog.body("huge")
    assert len(default) == MAX_BODY_CHARS


def test_the_body_excludes_the_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "clean",
        frontmatter="name: clean\ndescription: Tidy.",
        body="## Steps\n\nRead first.\n",
    )
    body = discover_skills(root).body("clean")
    assert body == "## Steps\n\nRead first."
    assert "description:" not in body


def test_an_unknown_name_yields_none_instead_of_an_error(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _simple(root, "known")
    catalog = discover_skills(root)

    assert catalog.body("unknown") is None
    assert catalog.get("unknown") is None
    # Der Name kommt womoeglich vom Modell und ist dann nicht einmal Text.
    assert catalog.body(None) is None
    assert catalog.body("../known") is None
    assert catalog.body({"name": "known"}) is None


def test_an_edited_skill_takes_effect_without_a_restart(tmp_path: Path) -> None:
    """Der Zwischenspeicher haengt am Dateizeitstempel, nicht an der Prozesslaufzeit —
    dieselbe Falle wie einst bei der SOUL, wo ein alter Name den Neustart ueberlebte."""
    root = tmp_path / "skills"
    folder = _write_skill(
        root, "edited", frontmatter="name: edited\ndescription: Same.", body="AAA\n"
    )
    catalog = discover_skills(root)
    assert catalog.body("edited") == "AAA"

    # Gleiche Laenge wie vorher: der Cache darf sich nicht auf die Groesse verlassen.
    (folder / "SKILL.md").write_text(
        "---\nname: edited\ndescription: Same.\n---\n\nBBB\n", encoding="utf-8"
    )
    assert catalog.body("edited") == "BBB"


def test_a_body_whose_file_vanished_yields_none(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    folder = _simple(root, "ghost")
    catalog = discover_skills(root)
    (folder / "SKILL.md").unlink()

    assert catalog.body("ghost") is None


# ---------------------------------------------------------------- Katalog


def test_the_catalog_shows_name_and_description(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _simple(root, "alpha", "Handles alphas.")
    _simple(root, "beta", "Handles betas.")

    rendered = discover_skills(root).render()

    assert "alpha" in rendered and "Handles alphas." in rendered
    assert "beta" in rendered and "Handles betas." in rendered
    # Progressive Disclosure: der Body bleibt draussen, bis er gebraucht wird.
    assert "## Steps" not in rendered


def test_the_catalog_respects_its_overall_cap_and_says_so(tmp_path: Path) -> None:
    """Regel 3: der Katalog geht in JEDEN Prompt — er darf ihn nicht auffressen,
    und wenn er kuerzt, sagt er es, statt still zu verschweigen."""
    root = tmp_path / "skills"
    for index in range(30):
        _simple(root, f"skill-{index:02d}", "A fairly wordy description. " * 4)

    rendered = discover_skills(root).render(max_chars=400)

    assert len(rendered) <= 400
    assert "omitted" in rendered
    assert "skill-00" in rendered


def test_a_long_description_is_shortened_per_line(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _simple(root, "wordy", "z" * (MAX_DESCRIPTION_CHARS - 1))

    line = discover_skills(root).get("wordy").catalog_line()
    prefix = "- wordy — "
    assert line.startswith(prefix)
    assert len(line) == len(prefix) + MAX_CATALOG_DESCRIPTION_CHARS
    assert line.endswith("…")


def test_an_empty_catalog_renders_to_nothing(tmp_path: Path) -> None:
    assert discover_skills(tmp_path).render() == ""
    assert SkillCatalog().render() == ""


def test_matching_skill_is_visible_before_the_catalog_limit_and_includes_its_path(tmp_path: Path) -> None:
    for index in range(40):
        _simple(tmp_path, f'alpha-{index:02d}', 'Generic unrelated procedure. ' * 4)
    wanted = _simple(tmp_path, 'server-plans', 'Compare VPS plans and infrastructure costs.')
    catalog = discover_skills(tmp_path)
    rendered = catalog.render(max_chars=500, query='Which VPS server plan should I choose?')
    assert len(rendered) <= 500 and 'omitted' in rendered
    assert 'server-plans' in rendered and str(wanted / 'SKILL.md') in rendered
    assert '## Steps' not in rendered


def test_live_skill_source_ranks_the_request_without_using_tool_result_instructions(tmp_path: Path) -> None:
    _simple(tmp_path, 'server-plans', 'VPS infrastructure plans.')
    _simple(tmp_path, 'alpha-unrelated', 'Unrelated example.')
    source = skills_module.SkillSource((tmp_path,))
    prompt = 'VPS server plans?\n[Tool results so far]\n' + 'alpha unrelated ' * 1000
    text = source.for_prompt(prompt)
    assert text.index('server-plans') < text.index('alpha-unrelated')
    _simple(tmp_path, 'new-vps', 'VPS provisioning.')
    assert 'new-vps' in source.for_prompt('VPS')


def test_reasoners_accept_ranked_and_legacy_skill_sources() -> None:
    from talos.reasoner import render_skill_source

    class Ranked:
        def for_prompt(self, prompt):
            assert prompt == 'server plans'
            return '- matching procedure'
    assert 'matching procedure' in render_skill_source(Ranked(), 'server plans')
    assert 'legacy procedure' in render_skill_source(lambda: '- legacy procedure')
    assert render_skill_source(lambda: 1 / 0) == ''


def test_the_skill_count_is_capped(tmp_path: Path) -> None:
    """Auch die Zahl der Skills ist eine Angriffsflaeche — 10 000 Verzeichnisse waeren
    kein Katalog mehr, sondern ein Denial of Service am Kontextfenster."""
    root = tmp_path / "skills"
    for index in range(6):
        _simple(root, f"skill-{index}")

    catalog = discover_skills(root, max_skills=3)

    assert len(catalog.skills) == 3
    assert len(catalog.rejected) == 3
    assert "skill limit of 3 reached" in _reason_for(catalog, "skill-5")


def test_the_skills_view_names_what_talos_refuses_to_obey(tmp_path: Path) -> None:
    """`/skills` muss sagen, dass `allowed-tools` ignoriert wird.

    Ein Skill darf vorschlagen, nie erlauben. Wenn ein Skill eine Vorab-Freigabe
    deklariert und der Betreiber das nirgends sieht, glaubt er im Zweifel dem Skill —
    und haelt eine Rueckfrage fuer einen Fehler statt fuer die Regel.
    """
    from talos.commands import CommandCenter

    root = tmp_path / "skills"
    _write_skill(root, "eager", frontmatter=(
        "name: eager\ndescription: Ok.\nallowed-tools: Bash(git:*) Read\n"
    ))
    catalog = discover_skills(root)
    eager = catalog.get("eager")
    assert eager is not None and eager.requested_tools

    view = CommandCenter._skills(
        type("Stub", (), {"skills_dirs": (root,)})()
    )
    assert "eager" in view
    assert "ignores that field" in view
    # Der deklarierte Wert selbst gehoert nicht in die Ansicht — nur die Tatsache.
    assert "Bash(git:*)" not in view
