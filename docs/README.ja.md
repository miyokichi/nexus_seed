# NEXUS SEED ドキュメント

初めて使う場合は、次の順番で読むと迷いにくくなります。

1. [はじめてのNEXUS SEED](getting-started.ja.md)
2. [Semantica Knowledgeの使い方](semantica.ja.md)
3. [困ったときの確認項目](troubleshooting.ja.md)

実装境界を確認したい開発者は、[MVP Application Flow](mvp.md)も参照してください。

## 目的別の入口

| やりたいこと | 読む場所 |
| --- | --- |
| とにかく起動したい | [最短の起動手順](getting-started.ja.md#最短の起動手順) |
| 自然文で仕事を依頼したい | [最初の依頼を送る](getting-started.ja.md#最初の依頼を送る) |
| ファイルを読ませたい | [ファイルをknowledgeへ入れる](getting-started.ja.md#ファイルをknowledgeへ入れる) |
| little_agentへ実作業を委譲したい | [A2Aでlittle_agentを使う](getting-started.ja.md#a2aでlittle_agentを使う) |
| Semanticaへ資料を投入したい | [サンプルを投入する](semantica.ja.md#サンプルを投入する) |
| 会社資料用のYAMLを作りたい | [Canonical YAML v0.1](semantica.ja.md#canonical-yaml-v01) |
| 起動や接続に失敗した | [トラブルシューティング](troubleshooting.ja.md) |

## 用語

| 用語 | このrepositoryでの意味 |
| --- | --- |
| Observation | 人、ファイル、外部systemから届いた観測 |
| Knowledge | 出典とrevisionを持つ記録 |
| World View | 現在有効な構造化factの投影 |
| Project | NEXUS SEEDが存在させ、状態を管理する仕事 |
| Project Agent | 1つのProjectを実際に進める実行者 |
| A2A | NEXUS SEEDと外部Agent Runtimeの通信境界 |
| Semantica | 業務概念、Property、Relationを扱うsemantic Knowledge backend |

