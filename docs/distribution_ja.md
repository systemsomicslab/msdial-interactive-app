# MS-DIAL Interactive 配布メモ

このアプリの標準配布形態は、各ユーザーが自分のPCで起動する
ローカルWebアプリです。

研究室サーバーで1つのアプリを共有する方式も技術的には可能ですが、
その場合、raw dataやライブラリーをサーバーから見える場所に置く必要が
あります。今回の想定では、各ユーザーのPCにあるデータを直接扱いたいので、
個人PCローカル起動を推奨します。

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
