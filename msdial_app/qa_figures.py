"""Quality-assurance figures as standalone SVG, drawn with the standard library only.

The LC-MS QA report is a table: the counts, ratios and criteria a reader checks a run
against. A person reviewing a run also wants to see it, and an agent asked for a picture
would otherwise write its own plotting code each time, at the cost of tokens and of
figures that differ from one analysis to the next. These are the figures, drawn the same
way every time, from the files a run and a laboratory lipidomics batch already write:

01 detected features per injection          from the QA matrix (*.qa.tsv)
02 summed peak height per injection         from the QA matrix
03 peak-height distribution per injection   from the QA matrix
04 internal-standard response               from the Height export and the standards normalize used
05 PCA of the study injections              from the QA matrix's reference-matched features
06 sample-to-Blank ratio per feature        from the QA matrix
07 automatic RT correction anchor offsets   from automatic_alignment_rt_correction_anchors.tsv
08 lipid class totals per sample            from the merged lipidome's class summary

Each figure is a function of parsed inputs that returns a :class:`FigureOutput`: SVG text,
or the reason it was not drawn. Nothing here reads or writes a file (qa_figure_outputs
does), and nothing depends on a clock, a random number or the platform's fonts: the same
inputs give the same bytes. Text widths are estimated from Helvetica's metrics, which
Arial shares, so that a label is shortened before it can leave its panel.

The look follows the laboratory's reference figures: a calm surface, recessive grid,
group colours from the reference categorical palette assigned in a fixed order over the
sorted class names, QC near-black, Blank grey and drawn as a cross.
"""

from __future__ import annotations

import html
import math
import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

# ---------------------------------------------------------------------------------------------
# Palette and type
# ---------------------------------------------------------------------------------------------

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e6e5e0"
ZERO_LINE = "#d6d5cf"
BAND = "#efeeea"
MUTED = "#8a8984"
CONNECTOR = "#c3c2b7"
BLANK_COLOR = "#8a8984"
QC_COLOR = "#3b3a36"
OTHER_COLOR = "#6f6e69"
# The reference categorical palette, in its fixed order (blue, orange, aqua, yellow, magenta,
# green, violet, red). No hue is generated beyond it: from the ninth study group on, the palette
# is reused with a second channel, the marker shape (SHAPES, eight groups each), so that every
# group stays distinct; groups beyond len(SERIES) * len(SHAPES) are drawn as "other".
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
SHAPES = ("dot", "square", "triangle", "diamond")
REFERENCE_MATCHED_COLOR = SERIES[0]
FONT_FAMILY = "Helvetica, Arial, 'Liberation Sans', sans-serif"

BLANK_GROUP = "Blank"
QC_GROUP = "QC"
UNLABELLED_GROUP = "(no class)"

TITLE_SIZE = 17.0
SUBTITLE_SIZE = 13.0
LEGEND_SIZE = 12.5
STRIP_SIZE = 12.0
TICK_SIZE = 11.5
AXIS_TITLE_SIZE = 13.0
NOTE_SIZE = 11.5
POINT_RADIUS = 4.5
MARGIN = 18.0

# log10 height bins of the distribution figure: quantiles at this resolution.
HEIGHT_BIN_WIDTH = 0.005
# The sample-to-Blank histogram's bin width (log10 units) and the ratio its share counts.
RATIO_BIN_WIDTH = 0.1
RATIO_THRESHOLD = 3.0
# Internal-standard tolerance band, in percent of the standard's mean.
STANDARD_BAND = (80.0, 120.0)
# Above this many study injections the PCA eigenproblem is solved by subspace iteration
# instead of Jacobi rotations, which are exact but cubic in pure Python.
JACOBI_LIMIT = 80
# Below this box spacing (px) figure 03 draws one outlier per side of a box and no grid line per box.
OUTLIER_BAND_LIMIT = 8.0
# Panels drawn per run in the RT-correction figure, at most.
RT_PANEL_LIMIT = 48
# Above this many injections in a PCA panel its points are not labelled (each keeps its tooltip).
PCA_LABEL_LIMIT = 30


# ---------------------------------------------------------------------------------------------
# Parsed inputs
# ---------------------------------------------------------------------------------------------


@dataclass
class Injection:
    """One injection of a run, as the QA matrix (or an alignment export) describes it."""

    file: str
    class_name: str
    file_type: str
    category: str  # Sample, QC or Blank, as quality_assurance classifies them
    order: int
    batch: int = 1
    label: str = ""

    @property
    def group(self) -> str:
        if self.category == "Blank":
            return BLANK_GROUP
        if self.category == "QC":
            return QC_GROUP
        return self.class_name or UNLABELLED_GROUP


@dataclass
class Anchor:
    reference_rt: float  # minutes
    offset_seconds: float
    used: bool


@dataclass
class RunQa:
    """What the figures need from one run's QA matrix, summarised while it is read once."""

    label: str
    injections: list[Injection]
    feature_count: int
    detected: list[int]
    summed: list[float]
    height_bins: list[dict[int, int]]
    blank_ratios: list[tuple[float, bool]] = field(default_factory=list)
    # The QA report's sample_blank_ratio_ge_3, exactly: (features at >= 3x, features counted), over the
    # features detected in a Blank and in a Sample, as median Sample height / median Blank height (> 0).
    blank_criterion: tuple[int, int] | None = None
    pca_matrix: list[list[float]] = field(default_factory=list)  # features x pca_injections
    pca_injections: list[int] = field(default_factory=list)  # indices into injections
    pca_reference_matched: int = 0
    pca_feature_limited: bool = False
    pca_sample_limited: bool = False
    non_numeric_cells: int = 0
    # RT correction audit, by injection file name; None when the run has no anchor file.
    anchors: dict[str, list[Anchor]] | None = None
    anchor_models: dict[str, str] = field(default_factory=dict)
    anchor_note: str = ""
    anchor_missing_reason: str = ""


@dataclass
class StandardResponse:
    """One internal standard's height in each injection of a polarity."""

    name: str
    alignment_id: str
    heights: list[float | None]  # aligned with the injections it is drawn against


@dataclass
class ClassTotals:
    """lipid_metabolome4classsummary.csv: one row per sample, one column per lipid class."""

    samples: list[str]
    groups: list[str]
    classes: list[str]
    values: list[list[float | None]]  # samples x classes


@dataclass(frozen=True)
class FigureOutput:
    key: str
    file_name: str
    title: str
    caption: str
    how_to_read: str
    svg: str | None = None
    skipped: str | None = None

    @property
    def drawn(self) -> bool:
        return self.svg is not None


class FigureSkipped(Exception):
    """A figure the inputs cannot support; the message says why, for the person."""


# ---------------------------------------------------------------------------------------------
# Figure catalogue: file names, titles, captions, how to read them
# ---------------------------------------------------------------------------------------------

FIGURES: dict[str, dict[str, str]] = {
    "01": {
        "file": "01_detected_features_by_injection.svg",
        "title": "Aligned features with a detected peak, per injection",
        "caption": "The number of aligned features with a peak in each injection (height > 0; gap-filled values count), in injection order.",
        "how": "Study injections should sit at a similar level. One injection well below its neighbours lost signal or was injected poorly; the Blank should sit far below the samples.",
    },
    "02": {
        "file": "02_summed_signal_by_injection.svg",
        "title": "Summed peak height per injection (run-order drift)",
        "caption": "The sum of all aligned feature heights in each injection, in injection order.",
        "how": "A steady rise or fall across the run is instrument drift; a single high or low injection points to that injection. The Blank should be far below the samples.",
    },
    "03": {
        "file": "03_intensity_distribution.svg",
        "title": "Peak-height distribution per injection",
        "caption": "The distribution of log10 peak height over the features detected in each injection, in injection order.",
        "how": "Each box spans the middle half of the heights with the median inside; whiskers reach 1.5 interquartile ranges and points beyond are drawn singly. Medians should line up across study injections; a shifted or compressed box suggests a loading or detector difference.",
    },
    "04": {
        "file": "04_internal_standards_{polarity}.svg",
        "title": "Internal-standard response across samples ({label})",
        "caption": "The height of each internal standard normalize used, as a percentage of its mean over the non-Blank injections, in injection order; each panel names the standard and its coefficient of variation.",
        "how": "Points inside the grey band are within 20% of the standard's mean. A standard drifting with injection order, or one injection low for every standard, points to a preparation or injection problem that normalization by that standard carries into every lipid of its classes.",
    },
    "05": {
        "file": "05_pca_injections.svg",
        "title": "PCA of injections (reference-matched features)",
        "caption": "Principal component scores of the study injections (Blank excluded) from the reference-matched features, log10(height + 1), autoscaled.",
        "how": "Injections of one group lie together only where the groups differ; QC injections, when present, should cluster tightly. An injection far from all others deserves a look before any statistics. The number after # is the injection order.",
    },
    "06": {
        "file": "06_sample_blank_ratio.svg",
        "title": "Sample-to-Blank ratio per aligned feature",
        "caption": "Each aligned feature's mean height over the Sample injections divided by its mean height in the Blank injections, as log10 of (sample + 1) / (blank + 1), with the share of features at or above 3x and, beside it, the QA report's criterion of the same name.",
        "how": "Features left of the dashed line are about as high in the Blank as in the samples and are likely background or carryover; blue marks the reference-matched ones. A feature with no Blank signal counts here, at or above 3x. The QA criterion (sample_blank_ratio_ge_3 in the QA results) is computed differently: median Sample over median Blank height, over the features detected in a Blank only, so it leaves out the features this figure counts as clean and is usually the lower of the two.",
    },
    "07": {
        "file": "07_rt_correction_offsets.svg",
        "title": "Automatic RT correction: anchor offsets per injection",
        "caption": "Each anchor's observed minus reference retention time, in seconds, against its reference retention time, per injection, from the Console's RT-correction audit.",
        "how": "Filled points are anchors the correction used, joined by a line; hollow points were not used (rejected as outliers or not matched well enough). Offsets of a few seconds on a smooth curve mean stable chromatography; a Blank borrows its nearest sample's correction.",
    },
    "08": {
        "file": "08_lipid_class_totals.svg",
        "title": "Lipid class totals per sample (merged lipidome)",
        "caption": "The sum of the quantified lipids in each lipid class, per sample, from the merged lipidome's class summary, on a log scale.",
        "how": "Classes are ordered by their median total, the largest at the top; each point is one sample in its group's colour. A difference between groups here is a first look, not a test; a class with one far point deserves a look at its lipids.",
    },
}

RUN_FIGURE_KEYS = ("01", "02", "03", "05", "06", "07")


def figure_info(key: str, **values: str) -> dict[str, str]:
    info = dict(FIGURES[key])
    for name in ("file", "title"):
        info[name] = info[name].format(**values) if values else info[name]
    return info


# ---------------------------------------------------------------------------------------------
# Colour, labels
# ---------------------------------------------------------------------------------------------


def group_sort_key(name: str) -> tuple[int, str, str]:
    special = {QC_GROUP: 1, BLANK_GROUP: 2}.get(name, 0)
    return (special, name.casefold(), name)


class GroupColors(dict):
    """Each group's colour (the mapping), and the marker shape that sets apart groups sharing one."""

    def __init__(self, colors: dict[str, str], shapes: dict[str, str]) -> None:
        super().__init__(colors)
        self.shapes = shapes


def group_colors(groups: Iterable[str]) -> GroupColors:
    """Each group's colour: the palette in its fixed order over the sorted study groups.

    The same groups give the same colours in every figure of a page, whichever figure
    shows which of them; QC is near-black and Blank grey whatever else is present. The
    ninth to sixteenth study groups reuse the palette with squares, the next eight with
    triangles, then diamonds (group_shape); beyond those a group is "other" grey.
    """
    names = sorted({name for name in groups if name not in (QC_GROUP, BLANK_GROUP)}, key=group_sort_key)
    styles = len(SERIES) * len(SHAPES)
    colors = {name: (SERIES[index % len(SERIES)] if index < styles else OTHER_COLOR) for index, name in enumerate(names)}
    shapes = {name: SHAPES[index // len(SERIES)] for index, name in enumerate(names) if index < styles}
    colors[QC_GROUP] = QC_COLOR
    colors[BLANK_GROUP] = BLANK_COLOR
    return GroupColors(colors, shapes)


def group_shape(colors: dict[str, str], group: str) -> str:
    """The marker of a group: a cross for the Blank, else the shape group_colors gave it (a dot)."""
    if group == BLANK_GROUP:
        return "cross"
    return getattr(colors, "shapes", {}).get(group, "dot")


def _luminance(color: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    channels = [int(color[index:index + 2], 16) / 255.0 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def ink_on(fill: str) -> str:
    """The line colour that stands out on a fill: the surface on a dark fill, near-black on a light one."""
    return SURFACE if contrast_ratio(SURFACE, fill) >= contrast_ratio(QC_COLOR, fill) else QC_COLOR


def ordered_groups(groups: Iterable[str]) -> list[str]:
    return sorted(set(groups), key=group_sort_key)


_TOKEN_SPLIT = re.compile(r"[_\-\s.]+")


def short_labels(injections: Sequence[Injection]) -> list[str]:
    """A short, unique label per injection: its group and the first distinguishing token.

    The tokens every file name shares at the start and at the end (a date, a project, the
    polarity) are dropped; of the rest, the fewest leading tokens that tell the files
    apart are kept. The group is put in front unless the label already names it, so that
    190722_TT1_Hclass_Aging_004_2_9w_SPF_M_Plasma_2_Pos becomes "SPF 004" and the Blank
    "Blank". Where nothing tells two files apart, the injection order does.
    """
    if not injections:
        return []
    tokens = [[part for part in _TOKEN_SPLIT.split(item.file) if part] or [item.file] for item in injections]
    if len(tokens) > 1:
        shortest = min(len(item) for item in tokens)
        lead = 0
        while lead < shortest - 1 and all(item[lead] == tokens[0][lead] for item in tokens):
            lead += 1
        trail = 0
        while (trail < shortest - lead - 1
               and all(item[len(item) - 1 - trail] == tokens[0][len(tokens[0]) - 1 - trail] for item in tokens)):
            trail += 1
        tokens = [item[lead: len(item) - trail] for item in tokens]
    longest = max(len(item) for item in tokens)
    chosen: list[str] | None = None
    for count in range(1, longest + 1):
        candidate = [" ".join(item[:count]) for item in tokens]
        if len(set(candidate)) == len(candidate):
            chosen = candidate
            break
    if chosen is None:
        chosen = [f"#{item.order}" for item in injections]
    labels = []
    for item, text in zip(injections, chosen):
        group = item.group
        folded = text.casefold()
        if group == UNLABELLED_GROUP or group.casefold() in folded.split() or group.casefold() == folded:
            labels.append(text)
        elif item.category == "Blank" and "blank" in folded:
            labels.append(text)
        else:
            labels.append(f"{group} {text}")
    if len(set(labels)) != len(labels):
        labels = [f"{label} #{item.order}" for label, item in zip(labels, injections)]
    return labels


# ---------------------------------------------------------------------------------------------
# Text metrics (Helvetica, which Arial shares), escaping, numbers
# ---------------------------------------------------------------------------------------------

_HELVETICA_WIDTHS = (
    # 32..126, per 1000 em
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = 0.0
    for char in text:
        code = ord(char)
        if 32 <= code <= 126:
            total += _HELVETICA_WIDTHS[code - 32]
        elif code >= 0x2E80:
            total += 1000  # CJK and other full-width characters
        else:
            total += 600
    return total / 1000.0 * size * (1.06 if bold else 1.0)


def fit_text(text: str, max_width: float, size: float, bold: bool = False) -> str:
    """The text, shortened with an ellipsis until it fits max_width."""
    if text_width(text, size, bold) <= max_width:
        return text
    ellipsis = "…"
    kept = text
    while kept and text_width(kept + ellipsis, size, bold) > max_width:
        kept = kept[:-1]
    return (kept.rstrip() + ellipsis) if kept else ""


def wrap_text(text: str, max_width: float, size: float, bold: bool = False, max_lines: int = 3) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}" if current else word
        if text_width(trial, size, bold) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [fit_text(line, max_width, size, bold) for line in lines]


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def escape(text: Any) -> str:
    """Text safe inside an XML element or attribute: escaped, and with no character XML forbids."""
    return html.escape(_CONTROL.sub("?", str(text)), quote=True)


def num(value: float) -> str:
    """A coordinate, at 0.01 px."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def decimals_for(step: float) -> int:
    for decimals in range(0, 8):
        if abs(round(step, decimals) - step) <= abs(step) * 1e-9:
            return decimals
    return 8


def format_tick(value: float, step: float) -> str:
    text = f"{value:.{decimals_for(step)}f}"
    if text.startswith("-") and float(text) == 0:
        text = text[1:]
    return text


def format_count(value: float) -> str:
    return f"{int(round(value)):,}"


def format_value(value: float) -> str:
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1e5 or magnitude < 1e-3:
        return f"{value:.3g}"
    if magnitude >= 100:
        return f"{value:,.0f}"
    return f"{value:.3g}"


# ---------------------------------------------------------------------------------------------
# Scales and ticks
# ---------------------------------------------------------------------------------------------


def nice_step(span: float, target: int = 5) -> float:
    if not span > 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(1, target)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for multiple in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= multiple * magnitude * (1 + 1e-9):
            return multiple * magnitude
    return 10.0 * magnitude


def linear_ticks(low: float, high: float, target: int = 5) -> tuple[list[float], float]:
    step = nice_step(high - low, target)
    first = math.ceil(low / step - 1e-9)
    last = math.floor(high / step + 1e-9)
    ticks = [index * step for index in range(first, last + 1)]
    return ticks, step


def nice_axis(low: float, high: float, target: int = 5) -> tuple[float, float, list[float], float]:
    """A domain that starts and ends on a tick, so that every value lies between two labelled lines.

    The step is chosen for [low, high]; the domain then widens to the multiples of the step around them.
    """
    step = nice_step(high - low, target)
    first = math.floor(low / step + 1e-9)
    last = math.ceil(high / step - 1e-9)
    if last <= first:
        last = first + 1
    return first * step, last * step, [index * step for index in range(first, last + 1)], step


def integer_ticks(low: float, high: float, target: int = 8) -> tuple[list[float], float]:
    """Ticks on whole numbers only, every one where few fit."""
    span = high - low
    step = max(1.0, nice_step(span, target))
    if step != int(step):
        step = math.ceil(step)
    first = math.ceil(low / step - 1e-9)
    last = math.floor(high / step + 1e-9)
    return [index * step for index in range(first, last + 1)], step


def padded(low: float, high: float, fraction: float = 0.05) -> tuple[float, float]:
    if not (math.isfinite(low) and math.isfinite(high)):
        return 0.0, 1.0
    if high <= low:
        pad = abs(high) * 0.1 or 1.0
        return low - pad, high + pad
    span = high - low
    return low - span * fraction, high + span * fraction


@dataclass
class Scale:
    low: float
    high: float
    start: float
    end: float

    def __call__(self, value: float) -> float:
        if self.high == self.low:
            return (self.start + self.end) / 2
        return self.start + (value - self.low) / (self.high - self.low) * (self.end - self.start)


# ---------------------------------------------------------------------------------------------
# The SVG canvas
# ---------------------------------------------------------------------------------------------


class Canvas:
    """Elements in drawing order; the height is known only once everything is placed."""

    def __init__(self, width: float) -> None:
        self.width = width
        self.parts: list[str] = []

    def add(self, element: str) -> None:
        self.parts.append(element)

    def text(
        self, x: float, y: float, content: str, *, size: float = TICK_SIZE, color: str = INK_SECONDARY,
        anchor: str = "start", bold: bool = False, rotate: float | None = None, tooltip: str | None = None,
        italic: bool = False,
    ) -> None:
        attributes = [f'x="{num(x)}"', f'y="{num(y)}"', f'font-size="{num(size)}"', f'fill="{color}"']
        if anchor != "start":
            attributes.append(f'text-anchor="{anchor}"')
        if bold:
            attributes.append('font-weight="bold"')
        if italic:
            attributes.append('font-style="italic"')
        if rotate is not None:
            attributes.append(f'transform="rotate({num(rotate)} {num(x)} {num(y)})"')
        inner = escape(content)
        if tooltip:
            inner += f"<title>{escape(tooltip)}</title>"
        self.add(f"<text {' '.join(attributes)}>{inner}</text>")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str, width: float = 1.0,
             dash: str | None = None) -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<line x1="{num(x1)}" y1="{num(y1)}" x2="{num(x2)}" y2="{num(y2)}" stroke="{color}" '
            f'stroke-width="{num(width)}"{extra}/>'
        )

    def rect(self, x: float, y: float, width: float, height: float, fill: str, *, stroke: str | None = None,
             stroke_width: float = 1.0, tooltip: str | None = None) -> None:
        extra = f' stroke="{stroke}" stroke-width="{num(stroke_width)}"' if stroke else ""
        body = f'x="{num(x)}" y="{num(y)}" width="{num(max(0.0, width))}" height="{num(max(0.0, height))}" fill="{fill}"{extra}'
        if tooltip:
            self.add(f"<rect {body}><title>{escape(tooltip)}</title></rect>")
        else:
            self.add(f"<rect {body}/>")

    def polyline(self, points: Sequence[tuple[float, float]], color: str, width: float = 1.3) -> None:
        if len(points) < 2:
            return
        coordinates = " ".join(f"{num(x)},{num(y)}" for x, y in points)
        self.add(
            f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="{num(width)}" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    def marker(self, x: float, y: float, shape: str, color: str, *, radius: float = POINT_RADIUS,
               tooltip: str | None = None, translucent: bool = False) -> None:
        """A point. translucent draws a filled shape at 55% opacity outlined in its own colour, so that
        points drawn over one another darken instead of hiding each other."""
        title = f"<title>{escape(tooltip)}</title>" if tooltip else ""
        paint = (f'fill="{color}" fill-opacity="0.55" stroke="{color}" stroke-width="1"' if translucent
                 else f'fill="{color}" stroke="{SURFACE}" stroke-width="1"')
        if shape == "cross":
            r = radius * 0.95
            path = (f"M{num(x - r)},{num(y - r)}L{num(x + r)},{num(y + r)}"
                    f"M{num(x - r)},{num(y + r)}L{num(x + r)},{num(y - r)}")
            self.add(f'<path d="{path}" stroke="{color}" stroke-width="1.6" fill="none">{title}</path>')
        elif shape == "ring":
            self.add(
                f'<circle cx="{num(x)}" cy="{num(y)}" r="{num(radius - 0.6)}" fill="{SURFACE}" stroke="{color}" '
                f'stroke-width="1.3">{title}</circle>'
            )
        elif shape == "square":
            r = radius * 0.9
            self.add(
                f'<rect x="{num(x - r)}" y="{num(y - r)}" width="{num(2 * r)}" '
                f'height="{num(2 * r)}" {paint}>{title}</rect>'
            )
        elif shape == "triangle":
            r = radius * 1.15
            path = (f"M{num(x)},{num(y - r)}L{num(x + r * 0.866)},{num(y + r * 0.5)}"
                    f"L{num(x - r * 0.866)},{num(y + r * 0.5)}Z")
            self.add(f'<path d="{path}" {paint}>{title}</path>')
        elif shape == "diamond":
            r = radius * 1.15
            path = f"M{num(x)},{num(y - r)}L{num(x + r)},{num(y)}L{num(x)},{num(y + r)}L{num(x - r)},{num(y)}Z"
            self.add(f'<path d="{path}" {paint}>{title}</path>')
        else:
            self.add(f'<circle cx="{num(x)}" cy="{num(y)}" r="{num(radius)}" {paint}>{title}</circle>')

    def render(self, height: float, key: str, title: str, description: str) -> str:
        identifier = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-") or "figure"
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(self.width)}" height="{num(height)}" '
            f'viewBox="0 0 {num(self.width)} {num(height)}" font-family="{escape(FONT_FAMILY)}" role="img" '
            f'aria-labelledby="qa-{identifier}-title qa-{identifier}-desc">'
        )
        lines = [
            head,
            f'<title id="qa-{identifier}-title">{escape(title)}</title>',
            f'<desc id="qa-{identifier}-desc">{escape(description)}</desc>',
            f'<rect x="0" y="0" width="{num(self.width)}" height="{num(height)}" fill="{SURFACE}"/>',
            *self.parts,
            "</svg>",
        ]
        return "\n".join(lines) + "\n"


def legend_width(items: Sequence[tuple[str, str, str]]) -> list[float]:
    return [14 + 7 + text_width(label, LEGEND_SIZE) for label, _, _ in items]


def draw_header(
    canvas: Canvas, left: float, right: float, title: str, subtitle: str,
    legend: Sequence[tuple[str, str, str]] = (), max_lines: int = 3,
) -> float:
    """Title, wrapped subtitle and a centred legend; returns where the body may start."""
    width = right - left
    y = 32.0
    canvas.text(left, y, fit_text(title, width, TITLE_SIZE, bold=True), size=TITLE_SIZE, color=INK, bold=True)
    for line in wrap_text(subtitle, width, SUBTITLE_SIZE, max_lines=max_lines) if subtitle else []:
        y += 20
        canvas.text(left, y, line, size=SUBTITLE_SIZE, color=INK_SECONDARY)
    if legend:
        widths = legend_width(legend)
        rows: list[list[int]] = [[]]
        used = 0.0
        for index, item_width in enumerate(widths):
            gap = 22 if rows[-1] else 0
            if rows[-1] and used + gap + item_width > width:
                rows.append([])
                used, gap = 0.0, 0
            rows[-1].append(index)
            used += gap + item_width
        y += 14
        for row in rows:
            y += 22
            total = sum(widths[index] for index in row) + 22 * (len(row) - 1)
            x = left + (width - total) / 2
            for index in row:
                label, color, shape = legend[index]
                if shape == "bar":
                    canvas.rect(x, y - 11, 14, 14, color)
                else:
                    canvas.marker(x + 7, y - 4, shape, color)
                canvas.text(x + 21, y + 0.5, label, size=LEGEND_SIZE, color=INK)
                x += widths[index] + 22
    return y + 26


def draw_strip(canvas: Canvas, x: float, width: float, y: float, text: str, *, tooltip: str | None = None,
               size: float = STRIP_SIZE) -> None:
    shown = fit_text(text, width - 4, size, bold=True)
    # A shortened title keeps its full text as the tooltip.
    canvas.text(x + width / 2, y, shown, size=size, color=INK, anchor="middle", bold=True,
                tooltip=tooltip or (text if shown != text else None))


def draw_note(canvas: Canvas, x: float, y: float, width: float, height: float, text: str) -> None:
    lines = wrap_text(text, width - 24, NOTE_SIZE, max_lines=4)
    top = y + height / 2 - (len(lines) - 1) * 8
    for index, line in enumerate(lines):
        canvas.text(x + width / 2, top + index * 16, line, size=NOTE_SIZE, color=INK_SECONDARY, anchor="middle")


def y_axis(canvas: Canvas, scale: Scale, ticks: Sequence[float], step: float, left: float, right: float,
           *, labels: bool = True, formatter: Callable[[float, float], str] = format_tick) -> None:
    for tick in ticks:
        y = scale(tick)
        canvas.line(left, y, right, y, GRID, 0.8)
        if labels:
            canvas.text(left - 6, y + 4, formatter(tick, step), color=INK_SECONDARY, anchor="end")


def x_axis(canvas: Canvas, scale: Scale, ticks: Sequence[float], step: float, top: float, bottom: float,
           *, labels: bool = True, formatter: Callable[[float, float], str] = format_tick) -> None:
    for tick in ticks:
        x = scale(tick)
        canvas.line(x, top, x, bottom, GRID, 0.8)
        if labels:
            canvas.text(x, bottom + 16, formatter(tick, step), color=INK_SECONDARY, anchor="middle")


def tick_label_width(ticks: Sequence[float], step: float,
                     formatter: Callable[[float, float], str] = format_tick) -> float:
    return max((text_width(formatter(tick, step), TICK_SIZE) for tick in ticks), default=0.0)


def group_legend(groups: Iterable[str], colors: dict[str, str]) -> list[tuple[str, str, str]]:
    """One entry per group in its colour and shape; groups beyond the palette's styles share one entry."""
    entries = []
    others = 0
    for name in ordered_groups(groups):
        color = colors.get(name, OTHER_COLOR)
        if color == OTHER_COLOR and name not in (QC_GROUP, BLANK_GROUP):
            others += 1
            continue
        entries.append((name, color, group_shape(colors, name)))
    if others:
        entries.append((f"{others} other group(s), not told apart", OTHER_COLOR, "dot"))
    return entries


def _figure(key: str, svg: str | None = None, skipped: str | None = None, **values: str) -> FigureOutput:
    info = figure_info(key, **values)
    return FigureOutput(
        key=key if not values.get("polarity") else f"{key}_{values['polarity']}",
        file_name=info["file"], title=info["title"], caption=info["caption"], how_to_read=info["how"],
        svg=svg, skipped=skipped,
    )


def _render(canvas: Canvas, height: float, key: str, title: str, caption: str) -> str:
    return canvas.render(height, key, title, caption)


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------


def jacobi_eigen(matrix: Sequence[Sequence[float]], max_sweeps: int = 100) -> tuple[list[float], list[list[float]]]:
    """Eigenvalues and eigenvectors (as columns of the second result) of a symmetric matrix.

    Cyclic Jacobi rotations until the off-diagonal mass is negligible; exact to rounding and
    deterministic, with no starting vector to choose.
    """
    size = len(matrix)
    a = [list(map(float, row)) for row in matrix]
    v = [[1.0 if row == column else 0.0 for column in range(size)] for row in range(size)]
    for _ in range(max_sweeps):
        off = sum(a[p][q] * a[p][q] for p in range(size) for q in range(p + 1, size))
        diagonal = sum(a[p][p] * a[p][p] for p in range(size))
        if off <= 1e-30 * max(diagonal, 1e-300) or off == 0.0:
            break
        for p in range(size - 1):
            for q in range(p + 1, size):
                apq = a[p][q]
                if apq == 0.0:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * apq)
                t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for row in a:
                    akp, akq = row[p], row[q]
                    row[p] = c * akp - s * akq
                    row[q] = s * akp + c * akq
                row_p, row_q = a[p], a[q]
                for k in range(size):
                    apk, aqk = row_p[k], row_q[k]
                    row_p[k] = c * apk - s * aqk
                    row_q[k] = s * apk + c * aqk
                for row in v:
                    vkp, vkq = row[p], row[q]
                    row[p] = c * vkp - s * vkq
                    row[q] = s * vkp + c * vkq
    values = [a[index][index] for index in range(size)]
    vectors = [[v[row][column] for row in range(size)] for column in range(size)]
    return values, vectors


def _orthonormal(vectors: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for vector in vectors:
        current = list(vector)
        for basis in result:
            projection = sum(map(operator.mul, current, basis))
            current = [value - projection * weight for value, weight in zip(current, basis)]
        length = math.sqrt(sum(value * value for value in current))
        if length > 1e-12:
            result.append([value / length for value in current])
    return result


def top_eigenpairs(matrix: Sequence[Sequence[float]], count: int, iterations: int = 3000) -> tuple[list[float], list[list[float]]]:
    """The largest eigenpairs of a symmetric positive semi-definite matrix, by subspace iteration.

    A deterministic start, a Rayleigh-Ritz step through jacobi_eigen each iteration, and a stop
    once the leading Ritz vectors no longer move. Used where the matrix is too large for Jacobi in
    pure Python.
    """
    size = len(matrix)
    width = min(size, count + 4)
    rows = [list(map(float, row)) for row in matrix]
    basis = _orthonormal([[math.sin((row + 1) * (column + 1) * 0.7) + (1.0 if row == column else 0.0)
                           for row in range(size)] for column in range(width)])
    previous: list[list[float]] = []
    values: list[float] = []
    for _ in range(iterations):
        products = [[sum(map(operator.mul, row, vector)) for row in rows] for vector in basis]
        basis = _orthonormal(products)
        images = [[sum(map(operator.mul, row, vector)) for row in rows] for vector in basis]
        projected = [[sum(map(operator.mul, left, image)) for image in images] for left in basis]
        ritz_values, ritz_vectors = jacobi_eigen(projected)
        order = sorted(range(len(ritz_values)), key=lambda index: -ritz_values[index])
        basis = [[sum(ritz_vectors[index][k] * basis[k][row] for k in range(len(basis))) for row in range(size)]
                 for index in order]
        values = [ritz_values[index] for index in order]
        leading = basis[:count]
        if previous and all(
            min(max(abs(a - b) for a, b in zip(new, old)), max(abs(a + b) for a, b in zip(new, old))) <= 1e-11
            for new, old in zip(leading, previous)
        ):
            break
        previous = [list(vector) for vector in leading]
    return values[:count], basis[:count]


def principal_components(matrix: Sequence[Sequence[float]], components: int = 2,
                         *, jacobi_limit: int = JACOBI_LIMIT) -> dict[str, Any]:
    """PCA of the samples (columns) over the features (rows), each feature autoscaled.

    A feature whose values are all equal carries no variance and is left out. The scores are
    the eigenvectors of the samples x samples Gram matrix of the autoscaled data times the
    square roots of their eigenvalues, which is the same as projecting the data on the
    loadings; the explained variance of a component is its eigenvalue over the Gram trace.
    Each component's sign is fixed so that its largest absolute score is positive.
    """
    rows = [list(map(float, row)) for row in matrix]
    samples = len(rows[0]) if rows else 0
    if samples < 3:
        raise FigureSkipped(f"PCA needs at least three study injections; there are {samples}")
    scaled: list[list[float]] = []
    for row in rows:
        if len(row) != samples or not all(math.isfinite(value) for value in row):
            continue
        if max(row) == min(row):
            continue
        mean = sum(row) / samples
        centered = [value - mean for value in row]
        deviation = math.sqrt(sum(value * value for value in centered) / (samples - 1))
        if not deviation > 0:
            continue
        scaled.append([value / deviation for value in centered])
    if len(scaled) < 2:
        raise FigureSkipped(
            f"PCA needs at least two features that vary across the study injections; there are {len(scaled)}"
        )
    columns = list(zip(*scaled))
    gram = [[0.0] * samples for _ in range(samples)]
    for i in range(samples):
        for j in range(i, samples):
            value = sum(map(operator.mul, columns[i], columns[j]))
            gram[i][j] = gram[j][i] = value
    trace = sum(gram[index][index] for index in range(samples))
    wanted = min(components, samples)
    if samples <= jacobi_limit:
        eigenvalues, eigenvectors = jacobi_eigen(gram)
        order = sorted(range(samples), key=lambda index: (-eigenvalues[index], index))[:wanted]
        values = [eigenvalues[index] for index in order]
        vectors = [eigenvectors[index] for index in order]
        method = "jacobi"
    else:
        values, vectors = top_eigenpairs(gram, wanted)
        method = "subspace_iteration"
    scores: list[list[float]] = []
    explained: list[float] = []
    for value, vector in zip(values, vectors):
        scale = math.sqrt(max(value, 0.0))
        component = [weight * scale for weight in vector]
        largest = max(range(samples), key=lambda index: (abs(component[index]), -index))
        if component[largest] < 0:
            component = [-item for item in component]
        scores.append(component)
        explained.append(max(value, 0.0) / trace if trace > 0 else 0.0)
    return {
        "scores": [list(item) for item in zip(*scores)],  # samples x components
        "explained": explained,
        "eigenvalues": values,
        "features": len(scaled),
        "samples": samples,
        "method": method,
    }


def relative_response(heights: Sequence[float | None]) -> dict[str, Any]:
    """Each height as a percentage of the mean over the heights given, and their CV (n - 1)."""
    present = [float(value) for value in heights if value is not None and math.isfinite(float(value))]
    if not present:
        return {"mean": None, "cv_percent": None, "relative": [None for _ in heights], "count": 0}
    mean = sum(present) / len(present)
    relative = [
        (float(value) / mean * 100.0 if mean > 0 and value is not None and math.isfinite(float(value)) else None)
        for value in heights
    ]
    cv = None
    if len(present) >= 2 and mean > 0:
        variance = sum((value - mean) ** 2 for value in present) / (len(present) - 1)
        cv = math.sqrt(variance) / mean * 100.0
    return {"mean": mean, "cv_percent": cv, "relative": relative, "count": len(present)}


def log_ratio(sample_mean: float, blank_mean: float) -> float:
    return math.log10((sample_mean + 1.0) / (blank_mean + 1.0))


def ratio_bin(value: float, width: float = RATIO_BIN_WIDTH) -> int:
    """The bin of a log ratio: bin k holds [k*width - width/2, k*width + width/2)."""
    return math.floor(value / width + 0.5)


def ratio_histogram(values: Iterable[tuple[float, bool]], width: float = RATIO_BIN_WIDTH) -> dict[int, list[int]]:
    """Counts per bin as [reference matched, not reference matched]."""
    bins: dict[int, list[int]] = {}
    for value, matched in values:
        if not math.isfinite(value):
            continue
        counts = bins.setdefault(ratio_bin(value, width), [0, 0])
        counts[0 if matched else 1] += 1
    return dict(sorted(bins.items()))


def share_at_least(values: Sequence[tuple[float, bool]], ratio: float = RATIO_THRESHOLD) -> float | None:
    finite = [value for value, _ in values if math.isfinite(value)]
    if not finite:
        return None
    threshold = math.log10(ratio)
    return sum(1 for value in finite if value >= threshold) / len(finite)


def height_bin(height: float) -> int:
    return math.floor(math.log10(height) / HEIGHT_BIN_WIDTH + 0.5)


def box_statistics(bins: dict[int, int]) -> dict[str, Any] | None:
    """Tukey box statistics of log10 heights binned at HEIGHT_BIN_WIDTH (quantiles of type 7).

    Values are the bin centres, so the statistics are exact for data on the bin grid and within
    half a bin otherwise. Outliers are the occupied bins beyond 1.5 interquartile ranges.
    """
    total = sum(bins.values())
    if total == 0:
        return None
    ordered = sorted(bins.items())

    def value_at(rank: int) -> float:
        seen = 0
        for index, count in ordered:
            seen += count
            if rank < seen:
                return index * HEIGHT_BIN_WIDTH
        return ordered[-1][0] * HEIGHT_BIN_WIDTH

    def quantile(fraction: float) -> float:
        position = (total - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        low_value = value_at(lower)
        if upper == lower:
            return low_value
        return low_value + (value_at(upper) - low_value) * (position - lower)

    q1, median_value, q3 = quantile(0.25), quantile(0.5), quantile(0.75)
    spread = q3 - q1
    low_fence, high_fence = q1 - 1.5 * spread, q3 + 1.5 * spread
    centres = [index * HEIGHT_BIN_WIDTH for index, _ in ordered]
    inside = [value for value in centres if low_fence - 1e-12 <= value <= high_fence + 1e-12]
    return {
        "count": total,
        "q1": q1,
        "median": median_value,
        "q3": q3,
        "whisker_low": min(inside) if inside else q1,
        "whisker_high": max(inside) if inside else q3,
        "outliers": [value for value in centres if value < low_fence - 1e-12 or value > high_fence + 1e-12],
        "min": centres[0],
        "max": centres[-1],
    }


# ---------------------------------------------------------------------------------------------
# Shared layout pieces
# ---------------------------------------------------------------------------------------------


def _left_margin(y_label: str) -> float:
    return MARGIN + (22 if y_label else 0)


def _axis_titles(canvas: Canvas, left: float, right: float, top: float, bottom: float, x_label: str,
                 y_label: str, x_offset: float = 40) -> float:
    if y_label:
        middle = (top + bottom) / 2
        canvas.text(MARGIN + 12, middle, fit_text(y_label, bottom - top, AXIS_TITLE_SIZE), size=AXIS_TITLE_SIZE,
                    color=INK_SECONDARY, anchor="middle", rotate=-90)
    if x_label:
        canvas.text((left + right) / 2, bottom + x_offset, fit_text(x_label, right - left, AXIS_TITLE_SIZE),
                    size=AXIS_TITLE_SIZE, color=INK_SECONDARY, anchor="middle")
        return bottom + x_offset + 18
    return bottom + 26


def _runs_with_injections(runs: Sequence[RunQa]) -> list[RunQa]:
    return [run for run in runs if run.injections]


def _x_positions(run: RunQa) -> tuple[list[float], str]:
    """Each injection's x: its injection order where those are unique, else its place in run order."""
    orders = [item.order for item in run.injections]
    if len(set(orders)) == len(orders) and all(order > 0 for order in orders):
        return [float(order) for order in orders], "Injection order"
    return [float(index + 1) for index in range(len(orders))], "Run position (batch, then injection order)"


def _page_colors(runs: Sequence[RunQa], extra: Iterable[str] = ()) -> dict[str, str]:
    return group_colors([item.group for run in runs for item in run.injections] + list(extra))


# ---------------------------------------------------------------------------------------------
# 01, 02: one value per injection
# ---------------------------------------------------------------------------------------------


def _per_injection_figure(
    key: str, runs: Sequence[RunQa], values_of: Callable[[RunQa], list[float]], y_label: str,
    subtitle: str, colors: dict[str, str] | None, tooltip: Callable[[Injection, float], str],
    divisor: float = 1.0,
) -> FigureOutput:
    info = figure_info(key)
    runs = _runs_with_injections(runs)
    if not runs:
        return _figure(key, skipped="the QA matrix lists no injection")
    colors = colors or _page_colors(runs)
    width = 900.0
    columns = min(2, len(runs))
    rows = math.ceil(len(runs) / columns)
    canvas = Canvas(width)
    left = _left_margin(y_label)
    groups = {item.group for run in runs for item in run.injections}
    top = draw_header(canvas, left, width - MARGIN, info["title"], subtitle, group_legend(groups, colors))
    facets = []
    label_width = 0.0
    for run in runs:
        values = [value / divisor for value in values_of(run)]
        low, high = padded(min(values), max(values))
        if low < 0 <= min(values):
            low = 0.0  # a count or a sum is never negative
        # The domain ends on ticks, so that the study injections are read between labelled lines.
        low, high, ticks, step = nice_axis(low, high, 6)
        xs, x_label = _x_positions(run)
        facets.append((run, values, low, high, ticks, step, xs, x_label))
        label_width = max(label_width, tick_label_width(ticks, step))
    gap = 18.0
    cell = (width - MARGIN - left - gap * (columns - 1)) / columns
    panel_height = 300.0 if rows == 1 else 220.0
    bottom = top
    for index, (run, values, low, high, ticks, step, xs, _) in enumerate(facets):
        column = index % columns
        row = index // columns
        panel_left = left + column * (cell + gap) + label_width + 8
        panel_right = left + column * (cell + gap) + cell
        strip_y = top + row * (panel_height + 52) + 14
        panel_top = strip_y + 10
        panel_bottom = panel_top + panel_height
        draw_strip(canvas, panel_left, panel_right - panel_left, strip_y, run.label)
        x_low, x_high = padded(min(xs), max(xs))
        x_scale = Scale(x_low, x_high, panel_left, panel_right)
        y_scale = Scale(low, high, panel_bottom, panel_top)
        x_ticks, x_step = integer_ticks(x_low, x_high, 10)
        x_axis(canvas, x_scale, x_ticks, x_step, panel_top, panel_bottom)
        y_axis(canvas, y_scale, ticks, step, panel_left, panel_right)
        points = [(x_scale(x), y_scale(value)) for x, value in zip(xs, values)]
        # The connector joins the study injections only; a Blank between them would make drift a zigzag.
        canvas.polyline(sorted(point for point, injection in zip(points, run.injections)
                               if injection.group != BLANK_GROUP), CONNECTOR, 1.3)
        spacing = (panel_right - panel_left) / max(1, len(points))
        radius = max(2.2, min(POINT_RADIUS, spacing * 0.42))
        for injection, (px, py), value in zip(run.injections, points, values):
            group = injection.group
            canvas.marker(px, py, group_shape(colors, group), colors.get(group, OTHER_COLOR),
                          radius=radius, tooltip=tooltip(injection, value * divisor))
        bottom = max(bottom, panel_bottom)
    x_label = facets[0][7]
    height = _axis_titles(canvas, left, width - MARGIN, top, bottom, x_label, y_label)
    return _figure(key, svg=_render(canvas, height, key, info["title"], info["caption"]))


def detected_features_figure(runs: Sequence[RunQa], colors: dict[str, str] | None = None) -> FigureOutput:
    """01: aligned features with a detected peak (height > 0) in each injection."""
    return _per_injection_figure(
        "01", runs, lambda run: [float(value) for value in run.detected], "Features",
        "Height > 0 in the aligned matrix; gap-filled values count as detected",
        colors, lambda item, value: f"{item.label} (injection {item.order}): {format_count(value)} features",
    )


def summed_height_figure(runs: Sequence[RunQa], colors: dict[str, str] | None = None) -> FigureOutput:
    """02: the sum of all aligned feature heights in each injection."""
    runs = _runs_with_injections(runs)
    largest = max((max(run.summed) for run in runs if run.summed), default=0.0)
    divisor, unit = (1e9, " (billions)") if largest >= 1e9 else (1e6, " (millions)") if largest >= 1e6 else (
        (1e3, " (thousands)") if largest >= 1e4 else (1.0, ""))
    return _per_injection_figure(
        "02", runs, lambda run: list(run.summed), f"Summed height{unit}",
        "Sum of all aligned feature heights", colors,
        lambda item, value: f"{item.label} (injection {item.order}): summed height {format_value(value)}",
        divisor=divisor,
    )


# ---------------------------------------------------------------------------------------------
# 03: distributions
# ---------------------------------------------------------------------------------------------


def height_distribution_figure(runs: Sequence[RunQa], colors: dict[str, str] | None = None) -> FigureOutput:
    """03: log10 peak-height boxplots per injection, in injection order, one panel per run."""
    key = "03"
    info = figure_info(key)
    runs = _runs_with_injections(runs)
    if not runs:
        return _figure(key, skipped="the QA matrix lists no injection")
    colors = colors or _page_colors(runs)
    stats = [[box_statistics(bins) for bins in run.height_bins] for run in runs]
    present = [item for run_stats in stats for item in run_stats if item]
    if not present:
        return _figure(key, skipped="no injection has a detected feature")
    low = min(min([item["min"]] + item["outliers"]) for item in present)
    high = max(max([item["max"]] + item["outliers"]) for item in present)
    low, high = padded(low, high)
    ticks, step = linear_ticks(low, high, 6)
    width = 900.0
    canvas = Canvas(width)
    y_label = "log10 height"
    left = _left_margin(y_label)
    groups = {item.group for run in runs for item in run.injections}
    # Groups that reuse a colour are told apart by a marker under each box, as in the other figures.
    shaped = any(group_shape(colors, name) not in ("dot", "cross") for name in groups)
    legend = [(name, colors.get(name, OTHER_COLOR), group_shape(colors, name) if shaped else "bar")
              for name in ordered_groups(groups)]
    panel_left = left + tick_label_width(ticks, step) + 8
    panel_right = width - MARGIN
    thinned = any((panel_right - panel_left) / len(run.injections) < OUTLIER_BAND_LIMIT for run in runs)
    subtitle = ("Aligned features with height > 0, in injection order"
                + (f"; where a box is narrower than {OUTLIER_BAND_LIMIT:g} px only its most extreme outlier on "
                   "each side is drawn" if thinned else ""))
    top = draw_header(canvas, left, width - MARGIN, info["title"], subtitle, legend)
    panel_height = 250.0 if len(runs) > 1 else 320.0
    y = top
    label_room = 24.0
    for run_index, (run, run_stats) in enumerate(zip(runs, stats)):
        strip_y = y + 14
        panel_top = strip_y + 10
        panel_bottom = panel_top + panel_height
        draw_strip(canvas, panel_left, panel_right - panel_left, strip_y, run.label)
        y_scale = Scale(low, high, panel_bottom, panel_top)
        y_axis(canvas, y_scale, ticks, step, panel_left, panel_right)
        count = len(run.injections)
        band = (panel_right - panel_left) / count
        narrow = band < OUTLIER_BAND_LIMIT
        box_width = min(70.0, band * 0.65)
        last = run_index == len(runs) - 1
        labels = [fit_text(item.label, 150, TICK_SIZE) for item in run.injections]
        upright = count <= 14 and all(text_width(label, TICK_SIZE) <= band - 4 for label in labels)
        # Labels rotated by 35 degrees stand 14 px apart when 14 / sin(35) of band separates them;
        # with less, every k-th is written.
        every = 1 if upright else max(1, math.ceil(14.0 / math.sin(math.radians(35)) / band))
        if last and not upright:
            label_room = max(text_width(label, TICK_SIZE) for label in labels) * 0.58 + 22
        for index, (injection, item) in enumerate(zip(run.injections, run_stats)):
            centre = panel_left + band * (index + 0.5)
            if not narrow:
                canvas.line(centre, panel_top, centre, panel_bottom, GRID, 0.8)
            if item:
                color = colors.get(injection.group, OTHER_COLOR)
                outliers = item["outliers"]
                if narrow and outliers:
                    # One point per side at most: a box this narrow cannot show the rest, and the
                    # figure (inlined in the page and the bundle) would grow with every injection.
                    median_value = item["median"]
                    below = [value for value in outliers if value < median_value]
                    above = [value for value in outliers if value > median_value]
                    outliers = ([min(below)] if below else []) + ([max(above)] if above else [])
                for value in outliers:
                    canvas.add(f'<circle cx="{num(centre)}" cy="{num(y_scale(value))}" r="1.4" fill="{MUTED}"/>')
                # Narrow boxes keep their fill visible: lighter whiskers and no outline.
                whisker = (MUTED, 0.6) if narrow else (INK_SECONDARY, 0.9)
                canvas.line(centre, y_scale(item["whisker_low"]), centre, y_scale(item["q1"]), *whisker)
                canvas.line(centre, y_scale(item["q3"]), centre, y_scale(item["whisker_high"]), *whisker)
                tooltip = (f"{injection.label} (injection {injection.order}): median {item['median']:.2f}, "
                           f"IQR {item['q1']:.2f} to {item['q3']:.2f}, {format_count(item['count'])} features")
                canvas.rect(centre - box_width / 2, y_scale(item["q3"]), box_width,
                            y_scale(item["q1"]) - y_scale(item["q3"]), color,
                            stroke=None if narrow else INK_SECONDARY, stroke_width=0.8, tooltip=tooltip)
                # The median in a colour that stands out on the box: light on QC near-black and other
                # dark fills, near-black on light ones.
                canvas.line(centre - box_width / 2, y_scale(item["median"]), centre + box_width / 2,
                            y_scale(item["median"]), ink_on(color), 1.8)
            if shaped:
                canvas.marker(centre, panel_bottom - 7, group_shape(colors, injection.group),
                              colors.get(injection.group, OTHER_COLOR), radius=min(3.2, max(1.6, band * 0.3)))
            if last and index % every == 0:
                if upright:
                    canvas.text(centre, panel_bottom + 16, labels[index], anchor="middle")
                else:
                    canvas.text(centre + 4, panel_bottom + 12, labels[index], anchor="end", rotate=-35,
                                tooltip=injection.file)
        y = panel_bottom + (8 if not last else 0)
    _axis_titles(canvas, panel_left, panel_right, top, y, "", y_label)
    height = y + label_room + 12
    return _figure(key, svg=_render(canvas, height, key, info["title"], info["caption"]))


# ---------------------------------------------------------------------------------------------
# 04: internal standards
# ---------------------------------------------------------------------------------------------


def internal_standard_figure(
    polarity: str, injections: Sequence[Injection], standards: Sequence[StandardResponse],
    colors: dict[str, str] | None = None, notes: Sequence[str] = (),
) -> FigureOutput:
    """04: each standard's height relative to its mean over the non-Blank injections, small multiples.

    notes are sentences the subtitle carries about the standards not drawn: those the standards table
    names but could not resolve in this polarity, and those resolved but not used by normalize.
    """
    key = "04"
    slug = polarity.casefold() or "run"
    info = figure_info(key, polarity=slug, label=polarity or "run")
    study = [index for index, item in enumerate(injections) if item.category != "Blank"]
    if not standards:
        reason = "normalize used no resolved internal standard in this polarity"
        if notes:
            reason += " (" + "; ".join(note.rstrip(".") for note in notes) + ")"
        return _figure(key, skipped=reason, polarity=slug, label=polarity or "run")
    if len(study) < 2:
        return _figure(key, skipped=f"a response across samples needs at least two non-Blank injections; there are {len(study)}",
                       polarity=slug, label=polarity or "run")
    colors = colors or group_colors(item.group for item in injections)
    ordered = sorted(standards, key=lambda item: (item.name.casefold(), item.name, item.alignment_id))
    panels = []
    values: list[float] = []
    for standard in ordered:
        heights = [standard.heights[index] if index < len(standard.heights) else None for index in study]
        response = relative_response(heights)
        panels.append((standard, response))
        values.extend(value for value in response["relative"] if value is not None)
    low, high = padded(min([STANDARD_BAND[0]] + values), max([STANDARD_BAND[1]] + values), 0.04)
    ticks, step = linear_ticks(low, high, 5)
    study_items = [injections[index] for index in study]
    orders = [item.order for item in study_items]
    if len(set(orders)) == len(orders) and all(order > 0 for order in orders):
        xs, x_label = [float(order) for order in orders], "Injection order"
    else:
        xs, x_label = [float(position + 1) for position in range(len(study))], "Run position"
    x_low, x_high = padded(min(xs), max(xs))
    x_ticks, x_step = integer_ticks(x_low, x_high, 7)
    columns = min(5, len(panels))
    rows = math.ceil(len(panels) / columns)
    width = 1050.0 if columns >= 4 else 900.0
    canvas = Canvas(width)
    y_label = "% of mean"
    left = _left_margin(y_label)
    groups = {item.group for item in study_items}
    subtitle = (f"Height relative to the standard's mean over the {len(study)} non-Blank injections; "
                f"band = +/- 20%; {len(panels)} standard{'' if len(panels) == 1 else 's'} used by normalize"
                + "".join(f". {note.rstrip('.')}" for note in notes))
    top = draw_header(canvas, left, width - MARGIN, info["title"], subtitle, group_legend(groups, colors),
                      max_lines=5)
    label_width = tick_label_width(ticks, step) + 8
    gap = 12.0
    cell = (width - MARGIN - left - label_width - gap * (columns - 1)) / columns
    panel_height = 170.0
    for index, (standard, response) in enumerate(panels):
        column = index % columns
        row = index // columns
        panel_left = left + label_width + column * (cell + gap)
        panel_right = panel_left + cell
        strip_top = top + row * (panel_height + 62)
        panel_top = strip_top + 36
        panel_bottom = panel_top + panel_height
        draw_strip(canvas, panel_left, cell, strip_top + 12, standard.name,
                   tooltip=f"{standard.name} (alignment ID {standard.alignment_id})")
        cv = response["cv_percent"]
        draw_strip(canvas, panel_left, cell, strip_top + 28, f"CV {cv:.0f}%" if cv is not None else "CV not computed",
                   size=STRIP_SIZE - 0.5)
        y_scale = Scale(low, high, panel_bottom, panel_top)
        x_scale = Scale(x_low, x_high, panel_left, panel_right)
        band_top, band_bottom = y_scale(min(high, STANDARD_BAND[1])), y_scale(max(low, STANDARD_BAND[0]))
        canvas.rect(panel_left, band_top, cell, band_bottom - band_top, BAND)
        y_axis(canvas, y_scale, ticks, step, panel_left, panel_right, labels=(column == 0))
        last_in_column = index + columns >= len(panels)
        x_axis(canvas, x_scale, x_ticks, x_step, panel_top, panel_bottom, labels=last_in_column)
        canvas.line(panel_left, y_scale(100.0), panel_right, y_scale(100.0), MUTED, 0.9)
        if response["mean"] is None or not response["mean"] > 0:
            draw_note(canvas, panel_left, panel_top, cell, panel_height, "no height in any non-Blank injection")
            continue
        points = []
        for injection, x, value, height in zip(study_items, xs, response["relative"],
                                               [standard.heights[i] if i < len(standard.heights) else None for i in study]):
            if value is None:
                continue
            points.append((x_scale(x), y_scale(value), injection, value, height))
        canvas.polyline([(px, py) for px, py, _, _, _ in points], CONNECTOR, 1.2)
        for px, py, injection, value, height in points:
            canvas.marker(px, py, group_shape(colors, injection.group), colors.get(injection.group, OTHER_COLOR),
                          radius=3.8,
                          tooltip=f"{injection.label} (injection {injection.order}): {value:.0f}% of mean, height {format_value(height or 0.0)}")
    bottom = top + rows * (panel_height + 62) - 26
    height = _axis_titles(canvas, left + label_width, width - MARGIN, top, bottom, x_label, y_label, x_offset=42)
    return _figure(key, svg=_render(canvas, height, f"{key}-{slug}", info["title"], info["caption"]),
                   polarity=slug, label=polarity or "run")


# ---------------------------------------------------------------------------------------------
# 05: PCA
# ---------------------------------------------------------------------------------------------


def run_pca(run: RunQa) -> dict[str, Any]:
    """The PCA of one run's study injections over its reference-matched features."""
    if len(run.pca_injections) < 3:
        raise FigureSkipped(f"PCA needs at least three study injections; there are {len(run.pca_injections)}")
    if not run.pca_matrix:
        raise FigureSkipped("no aligned feature is reference matched in a study injection")
    return principal_components(run.pca_matrix)


def _place_labels(
    points: Sequence[tuple[float, float, str]], left: float, right: float, top: float, bottom: float,
) -> list[tuple[float, float, str, str]]:
    """Label positions beside each point that overlap neither another label nor leave the panel."""
    placed: list[tuple[float, float, float, float]] = []
    result: list[tuple[float, float, str, str]] = []
    order = sorted(range(len(points)), key=lambda index: (points[index][1], points[index][0]))
    chosen: dict[int, tuple[float, float, str, str]] = {}
    for index in order:
        x, y, text = points[index]
        text = fit_text(text, max(40.0, (right - left) * 0.45), TICK_SIZE)
        width = text_width(text, TICK_SIZE)
        candidates = [
            (x + 8, y + 4, "start"), (x + 8, y + 16, "start"), (x + 8, y - 8, "start"),
            (x - 8, y + 4, "end"), (x - 8, y + 16, "end"), (x - 8, y - 8, "end"),
            (x + 8, y + 28, "start"), (x + 8, y - 20, "start"),
        ]
        best = None
        for cx, cy, anchor in candidates:
            x0 = cx if anchor == "start" else cx - width
            box = (x0, cy - 10, x0 + width, cy + 3)
            if box[0] < left or box[2] > right or box[1] < top or box[3] > bottom:
                continue
            if any(not (box[2] < other[0] or box[0] > other[2] or box[3] < other[1] or box[1] > other[3])
                   for other in placed):
                continue
            best = (cx, cy, anchor, box)
            break
        if best is None:
            anchor = "start" if x + 8 + width <= right else "end"
            cx = min(max(x + 8, left), right - width) if anchor == "start" else max(x - 8, left + width)
            cy = min(max(y + 4, top + 10), bottom - 3)
            x0 = cx if anchor == "start" else cx - width
            best = (cx, cy, anchor, (x0, cy - 10, x0 + width, cy + 3))
        placed.append(best[3])
        chosen[index] = (best[0], best[1], text, best[2])
    for index in range(len(points)):
        result.append(chosen[index])
    return result


def pca_figure(runs: Sequence[RunQa], colors: dict[str, str] | None = None) -> FigureOutput:
    """05: PC1/PC2 scores of the study injections from the reference-matched features, per run."""
    key = "05"
    info = figure_info(key)
    runs = _runs_with_injections(runs)
    if not runs:
        return _figure(key, skipped="the QA matrix lists no injection")
    colors = colors or _page_colors(runs)
    results: list[tuple[RunQa, dict[str, Any] | None, str]] = []
    for run in runs:
        try:
            results.append((run, run_pca(run), ""))
        except FigureSkipped as reason:
            results.append((run, None, str(reason)))
    if not any(result for _, result, _ in results):
        reasons = "; ".join(f"{run.label}: {reason}" if len(results) > 1 else reason for run, _, reason in results)
        return _figure(key, skipped=reasons)
    width = 1050.0 if len(results) > 1 else 760.0
    canvas = Canvas(width)
    left = _left_margin("PC2")
    groups = {run.injections[index].group for run, result, _ in results if result for index in run.pca_injections}
    counts = sorted({len(run.pca_injections) for run, result, _ in results if result})
    notes = []
    for run, result, _ in results:
        if result and run.pca_feature_limited:
            notes.append(f"{run.label}: the {result['features']} most variable of {run.pca_reference_matched} features")
        if result and run.pca_sample_limited:
            notes.append(f"{run.label}: {len(run.pca_injections)} study injections spread over the run order")
    labelled = all(len(run.pca_injections) <= PCA_LABEL_LIMIT for run, result, _ in results if result)
    subtitle = (f"{' / '.join(map(str, counts))} study injections, Blank excluded; log10(height + 1), autoscaled; "
                + ("label = group and sample, then #injection order" if labelled else
                   f"points are labelled up to {PCA_LABEL_LIMIT} injections; each point names itself on hover")
                + (f"; {'; '.join(notes)}" if notes else ""))
    top = draw_header(canvas, left, width - MARGIN, info["title"], subtitle, group_legend(groups, colors))
    columns = len(results)
    gap = 26.0
    cell = (width - MARGIN - left - gap * (columns - 1)) / columns
    panel_height = 330.0
    bottom = top
    for index, (run, result, reason) in enumerate(results):
        cell_left = left + index * (cell + gap)
        strip_y = top + 14
        panel_top = strip_y + 10
        panel_bottom = panel_top + panel_height
        if result is None:
            draw_strip(canvas, cell_left, cell, strip_y, run.label)
            draw_note(canvas, cell_left, panel_top, cell, panel_height, f"Not drawn: {reason}.")
            bottom = max(bottom, panel_bottom)
            continue
        explained = result["explained"] + [0.0] * (2 - len(result["explained"]))
        scores = result["scores"]
        pc1 = [row[0] for row in scores]
        pc2 = [row[1] if len(row) > 1 else 0.0 for row in scores]
        x_span = (max(pc1) - min(pc1)) or 1.0
        y_low, y_high = padded(min(pc2), max(pc2), 0.2)
        x_low, x_high = min(pc1) - 0.08 * x_span, max(pc1) + 0.3 * x_span
        y_ticks, y_step = linear_ticks(y_low, y_high, 6)
        x_ticks, x_step = linear_ticks(x_low, x_high, 6)
        label_width = tick_label_width(y_ticks, y_step) + 8
        panel_left = cell_left + label_width
        panel_right = cell_left + cell
        strip = (f"{run.label}  (PC1 {explained[0] * 100:.0f}%, PC2 {explained[1] * 100:.0f}%, "
                 f"{result['features']} features)")
        draw_strip(canvas, panel_left, panel_right - panel_left, strip_y, strip)
        x_scale = Scale(x_low, x_high, panel_left, panel_right)
        y_scale = Scale(y_low, y_high, panel_bottom, panel_top)
        x_axis(canvas, x_scale, x_ticks, x_step, panel_top, panel_bottom)
        y_axis(canvas, y_scale, y_ticks, y_step, panel_left, panel_right)
        if y_low < 0 < y_high:
            canvas.line(panel_left, y_scale(0), panel_right, y_scale(0), ZERO_LINE, 1.2)
        if x_low < 0 < x_high:
            canvas.line(x_scale(0), panel_top, x_scale(0), panel_bottom, ZERO_LINE, 1.2)
        injections = [run.injections[position] for position in run.pca_injections]
        points = [(x_scale(a), y_scale(b), f"{item.label} #{item.order}") for item, a, b in zip(injections, pc1, pc2)]
        labelled = len(points) <= PCA_LABEL_LIMIT
        labels = _place_labels(points, panel_left, panel_right, panel_top, panel_bottom) if labelled else []
        for (px, py, _), item, a, b in zip(points, injections, pc1, pc2):
            canvas.marker(px, py, group_shape(colors, item.group), colors.get(item.group, OTHER_COLOR),
                          radius=5.0 if labelled else 3.6,
                          tooltip=f"{item.label} (injection {item.order}): PC1 {a:.2f}, PC2 {b:.2f}")
        for lx, ly, text, anchor in labels:
            canvas.text(lx, ly, text, anchor=anchor, color=INK_SECONDARY)
        bottom = max(bottom, panel_bottom)
    height = _axis_titles(canvas, left, width - MARGIN, top, bottom, "PC1", "PC2")
    return _figure(key, svg=_render(canvas, height, key, info["title"], info["caption"]))


# ---------------------------------------------------------------------------------------------
# 06: sample-to-Blank ratio
# ---------------------------------------------------------------------------------------------


def sample_blank_ratio_figure(runs: Sequence[RunQa]) -> FigureOutput:
    """06: histogram of log10(mean sample / mean Blank) per aligned feature, stacked by reference match."""
    key = "06"
    info = figure_info(key)
    runs = _runs_with_injections(runs)
    usable = [run for run in runs if run.blank_ratios]
    if not usable:
        has_blank = any(item.category == "Blank" for run in runs for item in run.injections)
        has_sample = any(item.category == "Sample" for run in runs for item in run.injections)
        if not has_blank:
            return _figure(key, skipped="the run has no Blank injection")
        if not has_sample:
            return _figure(key, skipped="the run has no Sample injection to compare with its Blank")
        return _figure(key, skipped="the QA matrix has no aligned feature")
    histograms = {id(run): ratio_histogram(run.blank_ratios) for run in usable}
    all_bins = [index for histogram in histograms.values() for index in histogram]
    low = (min(all_bins) - 0.5) * RATIO_BIN_WIDTH
    high = (max(all_bins) + 0.5) * RATIO_BIN_WIDTH
    threshold = math.log10(RATIO_THRESHOLD)
    low, high = padded(min(low, threshold), max(high, threshold), 0.03)
    tallest = max(sum(counts) for histogram in histograms.values() for counts in histogram.values())
    y_low, y_high = 0.0, tallest * 1.12
    y_ticks, y_step = linear_ticks(y_low, y_high, 5)
    x_ticks, x_step = linear_ticks(low, high, 8)
    width = 900.0
    canvas = Canvas(width)
    y_label = "Features"
    left = _left_margin(y_label)
    legend = [("Reference matched", REFERENCE_MATCHED_COLOR, "bar"), ("Not reference matched", CONNECTOR, "bar")]
    sample_counts = sorted({sum(1 for item in run.injections if item.category == "Sample") for run in usable})
    subtitle = (f"Mean of the {' / '.join(map(str, sample_counts))} Sample injections over the Blank mean, per feature; "
                "a feature with no Blank signal counts at or above 3x here; dashed line = 3x. The QA criterion "
                "beside it is the QA report's: median heights, over the features detected in a Blank only")
    top = draw_header(canvas, left, width - MARGIN, info["title"], subtitle, legend)
    panel_left = left + tick_label_width(y_ticks, y_step) + 8
    panel_right = width - MARGIN
    panel_height = 240.0 if len(runs) > 1 else 320.0
    y = top
    for run_index, run in enumerate(runs):
        strip_y = y + 14
        panel_top = strip_y + 10
        panel_bottom = panel_top + panel_height
        last = run_index == len(runs) - 1
        x_scale = Scale(low, high, panel_left, panel_right)
        y_scale = Scale(y_low, y_high, panel_bottom, panel_top)
        draw_strip(canvas, panel_left, panel_right - panel_left, strip_y,
                   f"{run.label} ({format_count(len(run.blank_ratios))} features)" if run.blank_ratios else run.label)
        x_axis(canvas, x_scale, x_ticks, x_step, panel_top, panel_bottom, labels=last)
        y_axis(canvas, y_scale, y_ticks, y_step, panel_left, panel_right)
        if not run.blank_ratios:
            has_blank = any(item.category == "Blank" for item in run.injections)
            draw_note(canvas, panel_left, panel_top, panel_right - panel_left, panel_height,
                      "Not drawn: " + ("this run has no Blank injection." if not has_blank else "no Sample injection or feature."))
            y = panel_bottom + 6
            continue
        for index, (matched, unmatched) in histograms[id(run)].items():
            x0 = x_scale((index - 0.5) * RATIO_BIN_WIDTH)
            x1 = x_scale((index + 0.5) * RATIO_BIN_WIDTH)
            centre = index * RATIO_BIN_WIDTH
            base = y_scale(0)
            matched_top = y_scale(matched)
            total_top = y_scale(matched + unmatched)
            tooltip = (f"log10 ratio {centre - RATIO_BIN_WIDTH / 2:.2f} to {centre + RATIO_BIN_WIDTH / 2:.2f}: "
                       f"{matched} reference matched, {unmatched} not")
            if matched:
                canvas.rect(x0 + 0.4, matched_top, x1 - x0 - 0.8, base - matched_top, REFERENCE_MATCHED_COLOR,
                            tooltip=tooltip)
            if unmatched:
                canvas.rect(x0 + 0.4, total_top, x1 - x0 - 0.8, matched_top - total_top - (0.6 if matched else 0),
                            CONNECTOR, tooltip=tooltip)
        line_x = x_scale(threshold)
        canvas.line(line_x, panel_top, line_x, panel_bottom, INK, 1.0, dash="4 3")
        share = share_at_least(run.blank_ratios)
        lines = []
        if share is not None:
            lines.append((f"{share * 100:.0f}% of features at >= 3x by mean (no Blank signal counts as >= 3x)", INK))
        if run.blank_criterion is not None:
            at_least, counted = run.blank_criterion
            lines.append((
                (f"QA criterion (median, {format_count(counted)} Blank-detected features): {at_least / counted * 100:.0f}%"
                 if counted else "QA criterion: no feature detected in both a Blank and a Sample"), INK_SECONDARY))
        widest = max((text_width(text, 12.0) for text, _ in lines), default=0.0)
        # Beside the line, on whichever side has room for the text.
        anchor, text_x = ("start", line_x + 8) if line_x + 8 + widest <= panel_right - 4 else ("end", line_x - 8)
        for position, (text, color) in enumerate(lines):
            canvas.text(text_x, panel_top + 16 + 16 * position, fit_text(text, panel_right - panel_left - 16, 12.0),
                        color=color, size=12.0, anchor=anchor)
        y = panel_bottom + (6 if not last else 0)
    height = _axis_titles(canvas, panel_left, panel_right, top, y, "log10(sample / blank)", y_label)
    return _figure(key, svg=_render(canvas, height, key, info["title"], info["caption"]))


# ---------------------------------------------------------------------------------------------
# 07: RT correction anchors
# ---------------------------------------------------------------------------------------------


MODEL_NOTES = {
    "Reference": "reference file",
    "NearestBlank": "nearest sample's model",
    "Uncorrected": "not corrected",
}


def _evenly_spaced(count: int, limit: int) -> list[int]:
    if count <= limit:
        return list(range(count))
    return sorted({round(index * (count - 1) / (limit - 1)) for index in range(limit)})


def rt_correction_figure(runs: Sequence[RunQa], colors: dict[str, str] | None = None) -> FigureOutput:
    """07: observed minus reference RT of each anchor against its reference RT, per injection."""
    key = "07"
    info = figure_info(key)
    runs = _runs_with_injections(runs)
    with_anchors = [run for run in runs if run.anchors]
    if not with_anchors:
        reasons = [run.anchor_missing_reason or "the run has no automatic RT correction audit" for run in runs]
        reason = reasons[0] if len(set(reasons)) == 1 else "; ".join(
            f"{run.label}: {text}" for run, text in zip(runs, reasons))
        return _figure(key, skipped=reason or "the run has no automatic RT correction audit")
    rts = [anchor.reference_rt for run in with_anchors for anchors in run.anchors.values() for anchor in anchors]
    offsets = [anchor.offset_seconds for run in with_anchors for anchors in run.anchors.values() for anchor in anchors]
    if not rts:
        return _figure(key, skipped="the RT correction audit lists no anchor")
    x_low, x_high = padded(min(rts), max(rts))
    y_low, y_high = padded(min(offsets + [0.0]), max(offsets + [0.0]))
    x_ticks, x_step = linear_ticks(x_low, x_high, 4)
    y_ticks, y_step = linear_ticks(y_low, y_high, 5)
    label_sets = [[item.label for item in run.injections] for run in runs]
    grid = (len(runs) > 1 and all(labels == label_sets[0] for labels in label_sets)
            and len(label_sets[0]) <= 8)
    columns = len(label_sets[0]) if grid else min(8, max(len(run.injections) for run in runs))
    columns = max(1, columns)
    width = min(1400.0, max(900.0, 140.0 * columns + 120))
    canvas = Canvas(width)
    y_label = "Offset (s)"
    left = _left_margin(y_label)
    legend = [("Anchor used", REFERENCE_MATCHED_COLOR, "dot"), ("Anchor not used", REFERENCE_MATCHED_COLOR, "ring")]
    notes = [f"{run.label}: {run.anchor_note}" if len(runs) > 1 else run.anchor_note
             for run in runs if run.anchor_note]
    note = "; ".join(notes)
    subtitle = ("Observed minus reference RT of each anchor; a Blank uses its nearest sample's model"
                + (f". {note[:1].upper()}{note[1:]}" if note else ""))
    top = draw_header(canvas, left, width - MARGIN, info["title"], subtitle, legend)
    label_width = tick_label_width(y_ticks, y_step) + 8
    right_strip = 22.0 if grid else 0.0
    gap = 8.0
    cell = (width - MARGIN - left - label_width - right_strip - gap * (columns - 1)) / columns
    panel_height = 150.0
    omitted: list[str] = []

    def draw_panel(run: RunQa, injection: Injection, panel_left: float, panel_top: float, show_y: bool,
                   show_x: bool, title: str | None) -> None:
        panel_right = panel_left + cell
        panel_bottom = panel_top + panel_height
        if title is not None:
            draw_strip(canvas, panel_left, cell, panel_top - 10, title, size=STRIP_SIZE - 1)
        x_scale = Scale(x_low, x_high, panel_left, panel_right)
        y_scale = Scale(y_low, y_high, panel_bottom, panel_top)
        x_axis(canvas, x_scale, x_ticks, x_step, panel_top, panel_bottom, labels=show_x)
        y_axis(canvas, y_scale, y_ticks, y_step, panel_left, panel_right, labels=show_y)
        canvas.line(panel_left, y_scale(0), panel_right, y_scale(0), MUTED, 0.9)
        anchors = (run.anchors or {}).get(injection.file)
        model = run.anchor_models.get(injection.file, "")
        note = MODEL_NOTES.get(model, "")
        if not anchors:
            draw_note(canvas, panel_left, panel_top, cell, panel_height, "no anchors")
            return
        if note:
            canvas.text(panel_left + 4, panel_top + 12, fit_text(note, cell - 8, 10.5), size=10.5,
                        color=INK_SECONDARY, italic=True)
        used = sorted((anchor for anchor in anchors if anchor.used), key=lambda anchor: anchor.reference_rt)
        canvas.polyline([(x_scale(anchor.reference_rt), y_scale(anchor.offset_seconds)) for anchor in used],
                        REFERENCE_MATCHED_COLOR, 1.4)
        for anchor in sorted(anchors, key=lambda anchor: (anchor.reference_rt, anchor.offset_seconds)):
            canvas.marker(x_scale(anchor.reference_rt), y_scale(anchor.offset_seconds),
                          "dot" if anchor.used else "ring", REFERENCE_MATCHED_COLOR, radius=3.3,
                          tooltip=(f"{injection.label}: reference RT {anchor.reference_rt:.2f} min, offset "
                                   f"{anchor.offset_seconds:+.1f} s, {'used' if anchor.used else 'not used'}"))

    bottom = top
    if grid:
        for row, run in enumerate(runs):
            panel_top = top + 26 + row * (panel_height + 10)
            for column, injection in enumerate(run.injections):
                panel_left = left + label_width + column * (cell + gap)
                draw_panel(run, injection, panel_left, panel_top, column == 0, row == len(runs) - 1,
                           injection.label if row == 0 else None)
            strip_x = left + label_width + columns * (cell + gap) - gap + 14
            canvas.text(strip_x, panel_top + panel_height / 2, fit_text(run.label, panel_height, STRIP_SIZE, True),
                        size=STRIP_SIZE, color=INK, bold=True, anchor="middle", rotate=90)
            bottom = panel_top + panel_height
    else:
        y = top
        for run in runs:
            if len(runs) > 1:
                canvas.text(left, y + 16, run.label, size=STRIP_SIZE + 1, color=INK, bold=True)
                y += 22
            chosen = _evenly_spaced(len(run.injections), RT_PANEL_LIMIT)
            if len(chosen) < len(run.injections):
                omitted.append(f"{run.label}: {len(chosen)} of {len(run.injections)} injections, evenly spaced")
            rows = math.ceil(len(chosen) / columns)
            for position, index in enumerate(chosen):
                column = position % columns
                row = position // columns
                panel_left = left + label_width + column * (cell + gap)
                panel_top = y + 26 + row * (panel_height + 44)
                show_x = position + columns >= len(chosen)
                draw_panel(run, run.injections[index], panel_left, panel_top, column == 0, show_x,
                           run.injections[index].label)
            bottom = y + 26 + (rows - 1) * (panel_height + 44) + panel_height
            y = bottom + 30
    height = _axis_titles(canvas, left + label_width, width - MARGIN - right_strip, top, bottom,
                          "Reference RT (min)", y_label, x_offset=40)
    if omitted:
        canvas.text(left, height + 4, fit_text("; ".join(omitted), width - MARGIN - left, NOTE_SIZE),
                    size=NOTE_SIZE, color=INK_SECONDARY)
        height += 22
    return _figure(key, svg=_render(canvas, height, key, info["title"], info["caption"]))


# ---------------------------------------------------------------------------------------------
# 08: lipid class totals
# ---------------------------------------------------------------------------------------------


def _log_ticks(low: float, high: float) -> list[float]:
    first = math.floor(low)
    last = math.ceil(high)
    ticks = [float(power) for power in range(first, last + 1) if low - 1e-9 <= power <= high + 1e-9]
    return ticks


def _power_label(power: float) -> str:
    value = 10.0 ** power
    if -3 <= power <= 5:
        return f"{value:,.{max(0, int(-power))}f}" if power < 0 else f"{value:,.0f}"
    return f"1e{int(power)}"


def unit_label(unit: str) -> str:
    """A normalize unit as a reader writes it: pmol_per_microL_plasma -> pmol/uL plasma."""
    text = str(unit or "").strip()
    if not text:
        return ""
    text = text.replace("_per_", "/").replace("microL", "uL").replace("microg", "ug").replace("micro", "u")
    return text.replace("_", " ")


def lipid_class_totals_figure(table: ClassTotals, unit: str = "", colors: dict[str, str] | None = None) -> FigureOutput:
    """08: per-sample class totals of the merged lipidome, horizontal dot plot on a log scale."""
    key = "08"
    info = figure_info(key)
    entries: dict[str, list[tuple[float, str, str]]] = {}
    dropped = 0
    for sample, group, row in zip(table.samples, table.groups, table.values):
        for klass, value in zip(table.classes, row):
            if value is None or not math.isfinite(value) or value <= 0:
                dropped += 1
                continue
            entries.setdefault(klass, []).append((value, group or UNLABELLED_GROUP, sample))
    if not entries:
        return _figure(key, skipped="the class summary holds no positive concentration")

    def median(values: list[float]) -> float:
        ordered = sorted(values)
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2

    classes = sorted(entries, key=lambda name: (-median([value for value, _, _ in entries[name]]), name))
    colors = colors or group_colors(group for items in entries.values() for _, group, _ in items)
    groups = ordered_groups(group for items in entries.values() for _, group, _ in items)
    logs = [math.log10(value) for items in entries.values() for value, _, _ in items]
    low, high = padded(min(logs), max(logs), 0.04)
    ticks = _log_ticks(low, high)
    width = 820.0
    canvas = Canvas(width)
    class_width = min(150.0, max(text_width(name, TICK_SIZE) for name in classes) + 10)
    left = MARGIN + class_width
    unit_text = unit_label(unit)
    subtitle = ("Sum of quantified lipids in each class" + (f", {unit_text}" if unit_text else "") + ", log scale"
                + (f"; {dropped} empty or zero cell(s) left out" if dropped else ""))
    subtitle += "; each group has its own lane and its samples are spread across it, in sample order"
    top = draw_header(canvas, MARGIN, width - MARGIN, info["title"], subtitle, group_legend(groups, colors))
    # One lane per group in each class's row, and the group's samples spread over the lane: samples
    # with nearly equal totals stay separate points instead of one.
    lane = 14.0 if len(groups) <= 2 else 12.0
    row_height = max(22.0, lane * len(groups) + 4.0)
    panel_left = left
    panel_right = width - MARGIN
    panel_top = top
    panel_bottom = panel_top + row_height * len(classes)
    x_scale = Scale(low, high, panel_left, panel_right)
    for power in ticks:
        x = x_scale(power)
        canvas.line(x, panel_top, x, panel_bottom, GRID, 0.8)
        canvas.text(x, panel_bottom + 16, _power_label(power), anchor="middle")
    if high - low <= 4.5:
        for power in range(math.floor(low), math.ceil(high) + 1):
            for minor in (math.log10(2), math.log10(5)):
                value = power + minor
                if low < value < high:
                    x = x_scale(value)
                    canvas.line(x, panel_top, x, panel_bottom, GRID, 0.4)
    for row, klass in enumerate(classes):
        centre = panel_top + row_height * (row + 0.5)
        canvas.line(panel_left, centre, panel_right, centre, GRID, 0.8)
        canvas.text(panel_left - 8, centre + 4, fit_text(klass, class_width - 10, TICK_SIZE), anchor="end",
                    color=INK_SECONDARY)
        for lane_index, group in enumerate(groups):
            members = sorted((sample, value) for value, member_group, sample in entries[klass] if member_group == group)
            lane_centre = centre + (lane_index - (len(groups) - 1) / 2) * lane
            half = 0.32 * lane if len(members) > 1 else 0.0
            for position, (sample, value) in enumerate(members):
                offset = -half + 2 * half * position / (len(members) - 1) if len(members) > 1 else 0.0
                canvas.marker(x_scale(math.log10(value)), lane_centre + offset, group_shape(colors, group),
                              colors.get(group, OTHER_COLOR), radius=3.5, translucent=True,
                              tooltip=f"{klass}, {sample} ({group}): {format_value(value)}")
    x_label = unit_text or "Concentration"
    height = _axis_titles(canvas, panel_left, panel_right, panel_top, panel_bottom, x_label, "")
    return _figure(key, svg=_render(canvas, height, key, info["title"], info["caption"]))


# ---------------------------------------------------------------------------------------------
# A set of figures, and the page that shows them
# ---------------------------------------------------------------------------------------------


def safely(key: str, draw: Callable[[], FigureOutput], **values: str) -> FigureOutput:
    """The figure, its stated reason for not being drawn, or the error that stopped it."""
    try:
        return draw()
    except FigureSkipped as reason:
        return _figure(key, skipped=str(reason), **values)
    except Exception as error:  # a figure never stops the others, or the QA it accompanies
        return _figure(key, skipped=f"the figure could not be drawn ({type(error).__name__}: {error})", **values)


def run_figures(runs: Sequence[RunQa], colors: dict[str, str] | None = None) -> list[FigureOutput]:
    """Figures 01, 02, 03, 05, 06 and 07 for one run, or for several as panels side by side."""
    colors = colors or _page_colors(runs)
    return [
        safely("01", lambda: detected_features_figure(runs, colors)),
        safely("02", lambda: summed_height_figure(runs, colors)),
        safely("03", lambda: height_distribution_figure(runs, colors)),
        safely("05", lambda: pca_figure(runs, colors)),
        safely("06", lambda: sample_blank_ratio_figure(runs)),
        safely("07", lambda: rt_correction_figure(runs, colors)),
    ]


_PAGE_STYLE = """
:root { color-scheme: light; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --rule: #e6e5e0; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--ink);
  font: 15px/1.5 Helvetica, Arial, "Liberation Sans", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 6px; }
h2 { font-size: 18px; margin: 36px 0 8px; }
h3 { font-size: 16px; margin: 28px 0 6px; }
p { margin: 6px 0; }
.lede, .meta { color: var(--ink-2); }
figure { margin: 12px 0 0; padding: 0; }
figure svg { display: block; width: 100%; height: auto; border: 1px solid var(--rule); border-radius: 6px; }
figcaption { margin-top: 8px; }
.how { color: var(--ink-2); }
.skipped { border: 1px dashed #c3c2b7; border-radius: 6px; padding: 12px 14px; color: var(--ink-2); }
nav ul { padding-left: 20px; margin: 6px 0; }
code { font-size: 13px; }
"""


def report_html(title: str, lede: str, sections: Sequence[tuple[str, Sequence[FigureOutput]]],
                links: Sequence[tuple[str, str]] = ()) -> str:
    """A self-contained page: every drawn figure inline with its caption and how to read it."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title>",
        f"<style>{_PAGE_STYLE}</style>",
        "</head>",
        "<body>",
        "<main>",
        f"<h1>{escape(title)}</h1>",
        f'<p class="lede">{escape(lede)}</p>',
    ]
    if links:
        parts.append("<nav><ul>")
        parts.extend(f'<li><a href="{escape(href)}">{escape(text)}</a></li>' for text, href in links)
        parts.append("</ul></nav>")
    for heading, figures in sections:
        if heading:
            parts.append(f"<h2>{escape(heading)}</h2>")
        for figure in figures:
            parts.append(f'<section id="figure-{escape(figure.key)}">')
            parts.append(f"<h3>{escape(figure.title)}</h3>")
            if figure.svg:
                parts.append("<figure>")
                parts.append(figure.svg.strip())
                parts.append(
                    f"<figcaption><p>{escape(figure.caption)}</p>"
                    f'<p class="how"><strong>How to read it.</strong> {escape(figure.how_to_read)}</p>'
                    f'<p class="meta">File: <code>{escape(figure.file_name)}</code></p></figcaption>'
                )
                parts.append("</figure>")
            else:
                parts.append(f'<p class="skipped">Not drawn: {escape(figure.skipped or "no reason was given")}.</p>')
            parts.append("</section>")
    parts.extend(["</main>", "</body>", "</html>"])
    return "\n".join(parts) + "\n"
