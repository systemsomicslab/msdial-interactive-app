# MS-DIAL Interactive 配布メモ

## 初心者向けの推奨配布物

GitHub Actionsの `Build native desktop packages` を実行し、利用OSに対応する
artifactを配布します。Windowsは `MS-DIAL-Interactive.exe`、macOSは
`MS-DIAL-Interactive.app`、Linuxは `MS-DIAL-Interactive` 実行ファイルを
開くだけで起動でき、Pythonの事前インストールは不要です。MS-DIAL Consoleは
OSごとに別途用意し、初回のみPaths画面で指定します。

Windows artifact内のZIP、またはmacOS/Linux artifact内のtar.gzを展開して
使用します。macOS/Linuxをtar.gzにしているのは、実行権限を保持するためです。

ソースZIPを配布する場合も、ルート直下の `Start MS-DIAL Interactive.cmd`
（Windows）または `Start MS-DIAL Interactive.command`（macOS）から起動できます。

このアプリの標準配布形態は、各ユーザーが自分のPCで起動する
ローカルWebアプリです。

研究室サーバーで1つのアプリを共有する方式も技術的には可能ですが、
その場合、raw dataやライブラリーをサーバーから見える場所に置く必要が
あります。今回の想定では、各ユーザーのPCにあるデータを直接扱いたいので、
個人PCローカル起動を推奨します。

## 論文記載用ファイル

`7. Publication report` では、解析runの `workflow-settings.json` とQA結果から、
英語のMaterials and Methods案、QA Results案、Supplementary Table TSV、監査用JSONを
生成できます。文章は画面上で編集後、コピーまたはダウンロードできます。

Supplementary TableはExcel (`.xlsx`) が主出力です。`Data`にはMS-DIAL入力CSVと同じ
マトリックス、`Guided setup`にはセクション別のField/Value、`Annotation`にはannotator、
adduct、lipid query、library provenance、`Quality assurance`にはQA結果と基準を記録します。
従来のlong-format TSVも機械処理・監査用として同時に出力します。

新規runではMS-DIAL ConsoleとMS-DIAL Interactiveのバージョンを保存します。
旧runにバージョンが保存されていない場合、現在版を過去の解析版として推測せず、
`not recorded` と表示します。Zenodo catalogから取得したライブラリーはDOI、URL、
MD5、licenseを自動記録します。個別に用意したライブラリーは、VersionとDOIまたは
repository URLをPublication report画面で追記してください。

現在のセッションでQA matrixが未選択の場合は、解析run直下またはその直下のQA出力
サブフォルダーにある最新の `*.qa.tsv` を自動使用します。採用したファイルはPublication
report画面に表示します。vendor RAWフォルダーの内部までは再帰検索しません。

## 配布ZIPの作成

開発者PCで以下を実行します。

```bash
cd D:/0_SourceCode/msdial_interactive_app
python scripts/build-distribution.py
```

作成されるZIP:

```text
dist/msdial-interactive-app-local.zip
```

このZIPには、アプリ本体、resources、knowledge cards、起動スクリプト、
README、チュートリアルが含まれます。

以下は含めません。

- `.git`
- `.venv`
- `runs`
- `work`
- `dist`
- `__pycache__`
- raw data

## ユーザーに渡すもの

最低限:

- `msdial-interactive-app-local.zip`
- MS-DIAL Consoleの取得先
- Python 3.10以上が必要であること
- [local_user_tutorial_ja.md](local_user_tutorial_ja.md)

Windows初心者向けには、MS-DIAL ConsoleのZIPを展開した場所と
`MSDIALCUI.exe` の指定例を一緒に示すとよいです。

## 研究室内でのおすすめ運用

1. 研究室の共有場所に配布ZIPを置く
2. 各ユーザーは自分のPCにZIPを展開する
3. 各ユーザーは自分のPCでアプリを起動する
4. raw dataは各ユーザーPCまたは各ユーザーがマウントした共有ドライブ上に置く
5. 解析後、Export reusable workflow ZIPを保存する

この方式では、UIで指定したパスとMS-DIAL Console実行時のパスが同じPC内で
解決されるため、SCIEX `.wiff` と `.wiff.scan` の関係も自然に保てます。

## Windows/Mac/Linuxの違い

アプリ本体はPython標準ライブラリだけで動作します。
OS差が問題になりやすいのは、MS-DIAL Console本体とvendor raw readerです。

- Windows: ベンダーraw対応が最も現実的です。
- macOS/Linux: mzMLなどの標準形式を使う運用が最も安定します。
- Agilent/Waters/SCIEX/Brukerなどのvendor rawは、Consoleビルドとreader依存関係を確認してください。

## サーバー公開について

`scripts/start-lab-windows.ps1` と `scripts/start-lab-linux.sh` は残していますが、
これは上級者向けです。

サーバー公開では、ユーザーがブラウザから入力したパスはサーバー上のパスとして
解釈されます。各ユーザーPCのローカルデータを直接扱いたい場合には使わないでください。
