# Automatic Alignment RT Correction Audit

This viewer inspects a completed Console run. It does not change the run's
anchors, peak detections, or alignment results.

## Use

1. Enable automatic alignment-only RT correction for an LC-MS run and complete it.
2. Open **5. Validate & run** and select the completed job, or enter its output
   directory under **Automatic alignment RT correction audit**.
3. Click **Review RT correction**. Inspect the reference, model-source counts,
   rejected-anchor warnings, and method-key interpretation.
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
`automatic_alignment_rt_correction_anchors.tsv`. The viewer checks the method
hash against `method.keys.json` and checks for stale audit timestamps. EIC review
also requires matching `analysis_files.csv`, `run-manifest.json`, the original
raw data, and an available Console executable.

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
  or rejected smoothing settings leave only the raw curve and an explicit warning;
  another smoother is never substituted silently. The preview adds no dependency.
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
- `Missing` or `Ambiguous` candidates may have no original RT. They remain in
  the table as `N/A`, without preventing other candidates or files from being
  plotted. No missing RT is imputed, and apex-based EIC review is restricted to
  candidates with available RT values. If a candidate marked `Used` lacks RT,
  the affected file's model is not reconstructed from an incomplete subset.
- Blank-interpolated models are identified, but their full control points are
  absent from the current audit TSV. The viewer does not invent their curves.
- Method-key warnings are not hidden. Unsupported keys are listed separately
  from invalid values; some Console exports are controlled outside `method.txt`.

## Pre-Alignment Approval

The current CUI performs anchor selection and alignment in one invocation and
cannot pause for user approval. This viewer therefore has no approval button.
A future two-stage option should persist the selected RT models and the peak
results first, allow review, then resume alignment only after explicit approval.
The resume step must verify the input identities, method hash, feature outputs,
and approved models. It must not silently regenerate anchors after approval.
Unattended large-scale runs should retain the current one-pass option.

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
