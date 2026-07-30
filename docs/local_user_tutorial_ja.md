# MS-DIAL Interactive 個人PCローカル起動チュートリアル

このチュートリアルは、各ユーザーが自分のPC上で
MS-DIAL Interactiveを起動し、自分のPCにある質量分析データを解析する
ための手順です。

## 1. 事前に用意するもの

- Python 3.10以上
- MS-DIAL Console
- 解析したいraw data
- 必要に応じてMSP、LBM、Text DBなどのライブラリーファイル

Windowsでは、安定版Consoleとして
`MSDIAL.console.v5.5.260323-windows-net48` の `MSDIALCUI.exe` を指定します。
Mac/Linuxでは、使用するMS-DIAL Consoleビルドとraw data readerの対応状況に
注意してください。ベンダー依存の生データより、mzMLの方が移植性は高いです。

## 2. ZIPを展開する

配布されたZIPを、書き込み権限のある場所に展開してください。

例:

```text
Windows: C:\Users\<user>\Apps\msdial-interactive-app-local
macOS:   /Users/<user>/Apps/msdial-interactive-app-local
Linux:   /home/<user>/apps/msdial-interactive-app-local
```

raw dataをこのフォルダー内にコピーする必要はありません。
アプリは元のファイルパスを参照します。

## 3. アプリを起動する

### Windows

PowerShellで以下を実行します。

```powershell
cd C:\Users\<user>\Apps\msdial-interactive-app-local
.\scripts\start-local-windows.ps1
```

PowerShellに不慣れな場合は、`scripts\start-local-windows.cmd` を
ダブルクリックして起動することもできます。

MS-DIAL Consoleを先に指定する場合:

```powershell
.\scripts\start-local-windows.ps1 -ConsolePath "C:\MSDIAL\MSDIALCUI.exe"
```

PythonがPATHにない場合:

```powershell
.\scripts\start-local-windows.ps1 -PythonPath "C:\Python312\python.exe"
```

### macOS

Terminalで以下を実行します。

```bash
cd /Users/<user>/Apps/msdial-interactive-app-local
chmod +x scripts/start-local-macos.command scripts/start-local-linux.sh
./scripts/start-local-macos.command
```

### Linux

Terminalで以下を実行します。

```bash
cd /home/<user>/apps/msdial-interactive-app-local
chmod +x scripts/start-local-linux.sh
./scripts/start-local-linux.sh
```

起動すると、ブラウザで `http://127.0.0.1:8765` が開きます。
このURLはそのPC自身だけからアクセスするためのURLです。

## 4. データを追加する

Dataタブで以下のいずれかを使います。

- Add original files
- Add original folder
- Add path

フォルダー型データは、親フォルダーを指定すると検出できます。

- Waters `.raw`
- Agilent `.d`
- Bruker `.d`

SCIEXでは、解析行として追加するのは `.wiff` または `.wiff2` です。
`.wiff.scan` は同じフォルダーに置いたままにしてください。

## 5. パラメーターを設定する

Guided setup、Annotation、Tune parametersの順に設定します。

Tune parametersでは、代表ファイルを使って以下を調整できます。

- Minimum peak height
- Mass slice width
- MSP annotation thresholds

スライダーだけでなく数値入力欄にも直接入力できます。

Peak detectionでは、Smoothing methodも選択できます。
デフォルトは `LinearWeightedMovingAverage` です。

## 5a. GC-MSを解析する場合

Project typeで `GC-MS` を選択します。
GC-MSではTarget omicsはMetabolomicsに固定され、Solvent、Adduct、LBM、
Text DB、Lipid queryは表示されません。

GC-MS retention index settingsで以下を設定します。

- Accuracy type: Nominal GC-MSなら `IsNominal`、accurate mass取得なら `IsAccurate`
- RI compound type: Kovats indexなら `Alkanes`、Fiehn indexなら `Fames`
- Annotation retention type: annotationにRIを使うなら `RI`、RTのみなら `RT`
- Alignment index type: alignmentにRIを使うなら `RI`、RTのみなら `RT`
- RI dictionary source:
  `Use one carbon-RT file for all samples` を選ぶと、1つのalkane/FAME情報を
  全サンプルに割り当てる辞書ファイルをアプリが自動生成します。

Kovats RIのデモでは、`alkaneinfo.txt` のようなファイルを
`Alkane/FAME carbon-RT file` に指定します。

```text
Num	RT(min)
10	4.024
11	5.164
12	6.257
```

実行時には、CUIが必要とする `ri_dictionary_paths.txt` がrun folder内に
生成されます。このファイルはReusable workflow ZIPにも含まれます。

## 6. 実行する

Validate & runタブで以下を行います。

1. Validate workflow
2. Prepare only または Run MS-DIAL Console (-p)
3. 必要に応じて Export reusable workflow

Export reusable workflowでは、次回CUIだけで再実行するためのZIPが作成されます。
このZIPにはraw dataそのものは含まれません。

## 7. 終了する

ブラウザを閉じるだけではサーバープロセスは終了しません。
起動に使ったPowerShellまたはTerminalで `Ctrl+C` を押して終了します。

## 8. よくある注意点

- 入力パスは、アプリを起動している同じPCから見えるパスを指定します。
- ブラウザのドラッグ&ドロップは使いません。絶対パスを安定して取得できないためです。
- Agilent `.d` は、MS-DIAL Console側のAgilent readerとVC++ runtimeが必要な場合があります。
- Mac/Linuxでは、ベンダーraw readerの対応状況によりmzML変換が必要になることがあります。
