# Automatic Alignment RT Correction Audit

This viewer inspects a completed Console run, or an anchor preview. It does
not change a run's anchors, peak detections, or alignment results. For a
preview it lists every anchor for approval and writes the approved anchors as
the production run's anchor library.

## Use

1. Enable automatic alignment-only RT correction for an LC-MS run and complete it.
2. Open **5. Validate & run** and select the completed job, or enter its output
   directory under **Automatic alignment RT correction audit**.
3. Click **Review RT correction**. Inspect the reference, model-source counts,
   the correction evidence, the anchors the model rejected, and the method-key
   interpretation.
4. Choose a File ID. Compare the accepted and rejected candidates, the
   piecewise-linear shift curve, and anchor RT errors before/after the model.
5. Select an anchor and click **Extract selected EIC** to inspect the original
   chromatographic evidence near that candidate. The extraction uses the Console
   and raw paths recorded by this run. Raw data are neither copied nor changed.
   Compare the unsmoothed and smoothed traces on the original RT axis and on the
   projected alignment axis. Each trace can be hidden without extracting again.
6. Expand **Recorded anchor-selection criteria and score** and **Anchor
   correspondence and reversed order** to inspect the recorded settings and
   accepted/rejected RT pairings. Crossing connectors show reversed elution order.

Required audit files are `automatic_alignment_rt_correction_summary.tsv` and
`automatic_alignment_rt_correction_anchors.tsv`. Whether they prove this run's
correction is decided once, in `msdial_app/automatic_rt_evidence.py`, and the
viewer and the publication report both take that verdict. The records prove it
only when `method.keys.json` carries the hash of the `method.txt` beside it, the
Console applied the automatic RT correction key and discarded none of its
settings (as an unusable or a blank value), neither audit TSV is older than
`method.keys.json`, and a file other than the reference was corrected from its
own detected anchors. The viewer calls the evidence verified exactly when the
report would describe the correction; otherwise it shows the reason code the
report records in Supplementary Table S1. EIC review requires verified
evidence, and also matching `analysis_files.csv`, `run-manifest.json`, the
original raw data, and an available Console executable.

## Scientific Interpretation

- The corrected EIC is a **projection onto the alignment RT axis**, not a newly
  acquired or rewritten chromatogram. Its intensity is unchanged.
- Zero error at fitted anchors is a consequence of the model; it is not an
  independent demonstration of correct peak identity or alignment accuracy.
- Inspect peak shape and apex placement in the raw EIC. Compare rejected
  candidates and, for stronger validation, independent standards or held-out
  features not used to fit the model.
- The smoothing preview reads the run's `method.txt`, not the current UI settings.
  It applies smoothing to the full extracted EIC on its original RT axis before
  cropping or projection. The preview implements the CommonStandard formulas for
  `LinearWeightedMovingAverage`, `TimeBasedLinearWeightedMovingAverage`, and
  `SimpleMovingAverage`, including edge treatment. Other methods, missing levels,
  or a smoothing method or level the Console discarded (listed in
  `method.keys.json` as unusable or blank, even if another line applied it)
  leave only the raw curve and an explicit warning; another smoother is never
  substituted silently. The preview adds no dependency.
- A smooth-looking preview is not proof of a correct anchor. Stored shape metrics
  may originate from the initial, wider mass-slice EIC, whereas this review uses
  the displayed extraction tolerance. The preview does not recompute or overwrite
  the stored quality score. Component metrics are not in the current audit TSV.
- Initial quality gates apply to the reference candidates. Cross-file matching
  currently tests uniqueness within m/z and RT tolerances without reapplying all
  shape gates. Current score weights are 35% normalized log height, 20% saturated
  S/N, 15% Gaussian similarity, 15% ideal slope, 10% symmetry, and 5% width.
  This heuristic is not a calibrated probability; Gaussian similarity is based
  on areas rather than fitted Gaussian R-squared.
- A monotonic RT model preserves order: if sample RT increases, projected RT
  must also increase. For example, sample B=4.05 and A=4.25 versus reference
  A=4.00 and B=4.20 would give slope `(4.00-4.20)/(4.25-4.05)=-1`. After MAD filtering, the Console removes
  the lower-quality member of each conflicting pair and repeats; equal scores
  remove the earlier sample-RT member. Equal reference RTs are also rejected.
  `NonMonotonic` is a model constraint, not proof of incorrect identification:
  genuine selectivity changes can reverse compound elution order too.
- Several ions at exactly the same RT do not provide several independent RT
  control points. Duplicate positions can appear as `NonMonotonic` rejections.
- Unused anchor records fall into three groups, reported apart. Only
  `MadOutlier`, `NonMonotonic`, `InsufficientAnchors` (and any status the viewer
  does not know) are rejections by the model: they are counted, warned about,
  and highlighted in the tables and plots. `Missing` and `Ambiguous` records in
  a non-Blank file matched no single peak and never reached a model; they are
  listed in a note, not a warning. Every record in a Blank file
  (`BlankInterpolateByOrder`, `BlankNotCorrected`, or a `Missing` or `Ambiguous`
  one there) is unused by design: a Blank's own anchors never fit its model. They
  are listed in a note and drawn in grey.
- `Missing` or `Ambiguous` candidates have no original RT. They remain in
  the table as `N/A`, without preventing other candidates or files from being
  plotted. No missing RT is imputed, and apex-based EIC review is restricted to
  candidates with available RT values. A record of any other status without an
  RT is warned about. If a candidate marked `Used` lacks RT, the affected file's
  model is not reconstructed from an incomplete subset.
- Blank-interpolated models are identified, but their full control points are
  absent from the current audit TSV. The viewer does not invent their curves.
- Method-key warnings are not hidden. Unsupported keys are listed separately
  from invalid values; some Console exports are controlled outside `method.txt`.

## Anchor Modes, Preview And Approval (MsdialWorkbench#845)

A Console with #845 (5.5.261105 or later) takes the automatic correction's
anchors in one of three modes, set under **Anchors** in the automatic RT
correction panel:

- **Automatic** chooses them from the detected peaks, as before.
- **UserOnly** uses only the True rows of an anchor library. Each is found on
  every file's MS1 extracted ion chromatogram within its own m/z and RT window,
  one peak taken by the anchor peak selection mode.
- **Hybrid** uses the True rows and lets automatic anchors fill the RT bins they
  leave empty; the False rows keep automatic candidates within their m/z and RT
  tolerances out of the selection.

The library is the seven-column text `Name, RT(min), RT tol.(min), m/z, m/z
tol., Minimum height, T/F`, read as strictly as the Console reads it (see the
tool reference). A setting left at the Console default writes nothing, and any
of them with an older Console is refused.

The flow decided on 2026-11-07 is preview, then approval, then production:

1. **Preview anchors (no alignment)** runs the current method on every file
   with `Automatic RT correction preview only: True`, in a directory of its own
   under Output root (`.msdial-diagnostics/<job id>`). The Console picks peaks,
   chooses the anchors, writes the summary and anchors TSVs (and, in Hybrid,
   `automatic_alignment_rt_correction_exclusions.tsv`) and stops. Nothing is
   aligned, no annotation library is read, and nothing is written beside the raw
   data. The production output root's method file and audit are not touched.
2. The viewer opens the preview. **Anchor preview: accept or reject each
   anchor** lists every anchor once, user and automatic, with its source, name,
   library RT, reference RT, m/z, observed m/z, peak height, status, how many
   non-Blank files detected it, the outlier flag (the test disagreed with a user
   anchor, which is kept regardless) and the detection note; Hybrid adds the
   exclusions table. Each anchor must be accepted or rejected; no anchor is
   decided for you.
3. **Write approved anchor library** asks for your name and writes
   `automatic_rt_anchors_approved_<timestamp>.txt` (strict ASCII) into Output
   root, with `<library>.approval.json` beside it: who approved, when, the
   preview job, the SHA-256 of each preview audit file and the library's SHA-256.
   - An accepted automatic anchor becomes a True row at the reference file's
     observed RT and m/z, with the match RT tolerance as RT tolerance, the MS1
     tolerance for alignment as m/z tolerance, and a minimum height of 0.
   - A rejected automatic anchor becomes a False row at the same values.
   - An accepted user anchor keeps its row; a rejected one stays as a False row
     and is never deleted. The library's existing False rows stay.
4. The production run is set to **UserOnly** with the approved library. When
   the True rows are fewer than the minimum anchor count, UserOnly is refused
   and the viewer offers **Use Hybrid with this library** instead; nothing is
   chosen for you.

The production run's manifest and `workflow-settings.json` record the library
by path and SHA-256 with its approval, and the reproduction bundle carries both
files. The approval is claimed only while the library's SHA-256 is still the
approved one; a library edited afterwards runs as it is, with a warning, and the
Methods text does not call its anchors approved.

### The production run's reference file

An accepted automatic anchor is a True row at the RT it had in the preview's
reference file, so that file's RT axis is the one you reviewed. The preview
section of the viewer names it ("Reference file: C1 (file ID 2), chosen by
...") and the approval record keeps it as `preview_reference_file` (file ID,
name, and how the Console chose it).

A Console with MsdialWorkbench#876 chooses the reference again in UserOnly and
Hybrid, when no reference file ID is set: first the file with the most user
anchors detected, then the one whose detections lie closest to the library's
RTs. The preview's reference file lies at a deviation of about 0, so it is
chosen again. That Console's summary audit adds a `Reference selection` column
and a per-file `Library RT deviation (RT tolerances)` column, and the viewer
reads both. An older Console chose by how close each file's anchor RTs were to
their median over the batch. On demo data1 pos, Console 5.5.261105 previewed
on C1 and then ran on B1.

After a production run with an approved library, Interactive compares the
run's reference file with the approval's:

- Same file: a note in the review.
- Another file: a warning in the job's warnings, in the review and in the
  publication report's warnings. The run is not refused, because its
  correction is consistent, only on another file's axis.
  - Without the `Reference selection` column, the warning says the Console may
    predate the library-RT reference rule. Use a Console with it, or set the
    reference file ID to the preview's, and run again.
  - With the column, the warning names how the Console chose. For example, a
    file with more approved anchors detected wins before the closest one.
- When the preview's reference cannot be found (an approval written before this
  check, whose preview directory is gone), the review says it cannot be
  checked.

### How a preview audit is judged

A preview audit has `Run = Preview` in every summary row. The Console has chosen
the anchors and learned the maps but never applied them, so the audit is never
evidence of a correction, however complete it looks.
`automatic_rt_correction_proof` judges it by the same rules as a run (method
hash, applied and discarded keys, audit age) and then returns
`performed: false` with reason `preview_only`, its counts still filled in. The
viewer shows that as a note, not a warning, and the publication report describes
no correction from it. A summary without the `Run` column was written by a
Console before #845, which had no preview, and is judged as before. EIC review,
which needs verified evidence, is not available for a preview.

## Legacy User-Defined RT Correction

The point-by-point user-defined RT correction (anchors and reviewed peak
selections, applied before peak detection) is now in a closed **Legacy (not
recommended)** section of the main page. Worksets and templates that use it
load and run unchanged, it remains mutually exclusive with automatic RT
correction, and it gets no new features.

## Validation Record

On 2026-09-26 the CE-MS demo audit was inspected: three files, one reference,
two non-reference files with `DetectedAnchors`, and four rejected candidates.
An on-demand EIC returned 51 MS1 points near the first anchor. A zero-height
diagnostic of `20230208_SWATH_ASTD-ex_A_CE20.raw` produced 2,008 peaks with both
the existing Console and the rebuilt Console. The UI displayed the completed
diagnostic without the previous undefined-log-array error.

Focused checks passed: 27 Python tests and 25 C# ConfigParser tests. These are
focused regression checks, not a full scientific validation of anchor selection.

On 2026-09-27 the ten-file CE-MS audit was reviewed. Reference B1 anchor 2
(`m/z 195.05095`) returned 41 MS1 points in the review window. The LWMA level-3
preview apex was RT 4.8813948 min with intensity 178974.0625, matching the saved
feature. All 1,777 full-EIC points were compared against the actual Common.dll
implementation for the three supported preview methods: maximum absolute
differences were 0 (LWMA), 4.95e-10 (time-based LWMA), and 4.37e-10 (SMA).
The browser's trace visibility controls and correspondence diagram were checked.
All 424 Python tests and the JavaScript viewer regression checks passed; on
Windows, the shell reproduction test used Git Bash rather than the WSL stub.
No anchor-selection or alignment algorithm was changed in this viewer update.

## RawDataHandler Packages And Build Configuration

The private `msrawdataworkbench/RawDataHandlerStandard/RawDataHandlerStandard.csproj`
produces two distinct packages:

- `Release`: `RawDataHandler`, with `SUPPORT_VENDOR_FORMAT` enabled. It includes
  the vendor-reader implementation as compiled DLLs and packages vendor runtime
  dependencies. This is the package used for vendor RAW support in MS-DIAL
  distribution builds; vendor parser source is not public.
- `Release-VendorUnSupported`: `RawDataHandler-Vendor-UnSupported`, excluding
  vendor parser source from compilation. It supports the public-source build
  without the proprietary vendor readers.

After parser changes, the maintainer's release workflow updates both packages.
Building MS-DIAL consumes these packages; it does not itself rebuild or publish
the RawDataHandler packages. Select and restore the supported package for tests
using Thermo, SCIEX, Agilent, Bruker, Shimadzu, or Waters vendor data. Do not treat
a manually copied DLL as a substitute for restoring the correct dependency.

Producer configuration names use hyphens, as shown above. The corresponding
MS-DIAL consumer configurations use `Release` or `Release vendor unsupported`.

When switching C# build configurations, restore dependencies for that same
configuration before using `--no-restore`. Otherwise a prior vendor-unsupported
restore can produce a Release binary that cannot read vendor RAW data:

```powershell
dotnet restore tests/MSDIAL5/MsdialCoreTestApp/MsdialCoreTestApp.csproj -p:Configuration=Release
dotnet build tests/MSDIAL5/MsdialCoreTestApp/MsdialCoreTestApp.csproj -c Release -f net48 --no-restore
```

The Interactive developer-build action uses normal `dotnet build` with implicit
restore for the selected configuration.
