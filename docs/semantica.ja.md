# Semantica Knowledgeの使い方

Semanticaは「業務世界について何を知っているか」を扱うKnowledge部品です。NEXUS SEEDからは`nexus_knowledge`のadapterを通して使用し、PlannerやProject AgentがSemantica固有objectを直接扱うことはありません。

```text
資料
  -> Canonical YAML v0.1
  -> nexus_knowledge
  -> Semantica
  -> Relevant Knowledge
  -> Planning Context
```

## インストールを確認する

repository rootで実行します。

```powershell
uv sync --extra dev --extra semantica
uv run python -c "import sys, semantica; print(sys.version); print(semantica.__version__)"
```

このprojectはPython 3.13を既定にしています。Python 3.14ではSemanticaの依存packageに対応wheelがないため使用しません。

確認できるpackage:

```powershell
uv pip list | Select-String -Pattern "semantica|gensim"
```

## サンプルを投入する

### 1. 保存先を設定する

`.env`へ追加します。

```dotenv
NEXUS_SEED_SEMANTICA_SNAPSHOT=~/.nexus_seed/semantica-knowledge.json
NEXUS_SEED_SEMANTICA_ONTOLOGY=modules/knowledge/samples/ontology_v0.1.yaml
```

snapshotはSemantica graphと投入済みCanonical documentを保持するfileです。repository外へ置いてください。

### 2. Canonical YAMLをingestする

```powershell
uv run nexus-seed-semantica ingest `
  modules/knowledge/samples/assumption_v0.1.yaml
```

成功例:

```json
{
  "entities": 3,
  "relations": 2,
  "snapshot": ".../semantica-knowledge.json"
}
```

`.env`を使わず、その場で保存先とOntologyを指定することもできます。global optionは`ingest`より前に置きます。

```powershell
uv run nexus-seed-semantica `
  --snapshot "$HOME/.nexus_seed/semantica-knowledge.json" `
  --ontology modules/knowledge/samples/ontology_v0.1.yaml `
  ingest modules/knowledge/samples/assumption_v0.1.yaml
```

### 3. queryする

```powershell
uv run nexus-seed-semantica query "GenX WL Width"
```

query結果には次が含まれます。

- 一致したEntity
- EntityのProperty
- 1-hopのexplicit Relation
- 分離されたinferred Relation
- documentとlocationのprovenance

結果件数を制限する場合:

```powershell
uv run nexus-seed-semantica query "WL Width" --limit 10
```

## Canonical YAML v0.1

Semanticaへ直接、資料形式ごとの独自objectを渡しません。plain text、Excel、PowerPointなどは、すべて一度Canonical YAMLへ変換します。

```yaml
schema_version: "0.1"

document:
  id: "assumption-001"
  source:
    file: "assumptions.xlsx"
    location: "Sheet1"

content:
  text: |
    GenX uses WL Width.
    WL Width has a typical value of 50 nm.

entities:
  - id: "gen_x"
    name: "GenX"
    type: "Generation"
    aliases: []
    properties: {}
    source:
      document_id: "assumption-001"
      location: "Sheet1!A2"

  - id: "wl_width"
    name: "WL Width"
    type: "Parameter"
    aliases:
      - "WL width"
    properties:
      typical: 50
      variation: 10
      unit: "nm"
    source:
      document_id: "assumption-001"
      location: "Sheet1!B2:E2"

relations:
  explicit:
    - subject: "gen_x"
      predicate: "uses_parameter"
      object: "wl_width"
      source:
        document_id: "assumption-001"
        location: "Sheet1!A2:E2"
```

### 必須項目

| 場所 | 必須内容 |
| --- | --- |
| root | `schema_version`, `document`, `content`, `entities`, `relations` |
| document | `id`, `source.file`, `source.location` |
| content | `text` |
| entity | `id`, `name`, `type`; `aliases`はlist、`properties`はmapping |
| relation | `subject`, `predicate`, `object` |

Relationの`subject`と`object`は、同じdocumentの`entities.id`を参照する必要があります。不正なYAMLはSemanticaへ送られる前に拒否されます。

## EntityとPropertyの分け方

Entityは、他の対象とRelationを持ち、独自の同一性を持つ業務概念です。

```text
Generation
Process
Parameter
Document
Deliverable
```

PropertyはEntityを説明する値です。

```text
typical
variation
unit
condition
description
status
```

数値や単位を独立Entityにしません。

```text
正しい:
WL Width
  typical = 50
  variation = 10
  unit = nm

避ける:
WL Width -> has_typical -> 50nm Entity
```

## explicitとinferred

Canonical YAMLへ書くのは資料または人が明示した`relations.explicit`だけです。

Semanticaによるrelation推論を試す場合:

```powershell
uv run nexus-seed-semantica ingest `
  modules/knowledge/samples/assumption_v0.1.yaml `
  --infer-relations
```

query結果では次のように分離されます。

```json
{
  "relations": {
    "explicit": [],
    "inferred": []
  }
}
```

inferred Relationは確定業務factへ自動昇格しません。利用側はconfidence、method、sourceを確認できます。

## Ontologyを変更する

sample Ontology:

```text
modules/knowledge/samples/ontology_v0.1.yaml
```

構造:

```yaml
version: "0.1"
entity_types:
  - Generation
  - Parameter
  - Deliverable
relation_types:
  - uses_parameter
  - requires
```

会社では最初から巨大なOntologyを作らず、1テーマに必要なEntity typeとRelation typeだけ追加します。Vocabulary外のtypeはingest時に拒否されます。`related_to`だけへまとめず、業務上の意味がある少数のpredicateを使います。

## 会社資料へ置き換える

最初の試験は、1世代、1テーマ、数資料に限定します。

1. 資料から人が読めるtextを抽出する
2. Relation対象になる概念をEntityにする
3. 数値、単位、状態をPropertyにする
4. 資料が明示したRelationだけ`explicit`へ書く
5. documentとlocationを必ず付ける
6. sampleと同じCLIでingestする
7. 主要Entity名でqueryする
8. 結果の値と原典位置を人が確認する

資料adapterの責務は`資料 -> Canonical YAML`までです。Excel用adapterやPowerPoint用adapterからSemanticaを直接呼びません。

## NEXUS SEEDとの接続範囲

Semantica adapterは`nexus_knowledge.SemanticaKnowledgeAdapter`として公開され、`ExistingKnowledgeGateway`の`semantic_backend`へ渡します。

```python
from pathlib import Path

from nexus_seed.modules.knowledge import (
    ExistingKnowledgeGateway,
    SemanticaKnowledgeAdapter,
    load_ontology_yaml,
)

semantic = SemanticaKnowledgeAdapter(
    Path.home() / ".nexus_seed" / "semantica-knowledge.json",
    ontology=load_ontology_yaml("modules/knowledge/samples/ontology_v0.1.yaml"),
)
knowledge = ExistingKnowledgeGateway(ledger, semantic_backend=semantic)
```

Plannerへ渡るのはSemantica objectではなく、`entities`、`properties`、`relations`、`sources`へ正規化されたRelevant Knowledgeです。完全な閉ループ例は`tests/test_semantica_closed_loop_e2e.py`にあります。

現在の`NEXUS_SEED_SEMANTICA_*`設定を直接読むのは`nexus-seed-semantica` CLIです。常駐`nexus-seed` Applicationへ任意のsnapshotを自動接続するconfigurationはまだ追加していません。常駐Applicationで利用する場合は、上記public adapterをcomposition時に渡します。

## snapshotの扱い

- process終了後もqueryできます
- 同じ`document.id`を再ingestすると、そのdocumentの現在内容でgraphを再構築します
- snapshotをbackupすれば投入済みdocumentとgraphを一緒に保存できます
- 壊れたsnapshotを黙って読み替えず、format不一致として拒否します

本番DBの代替ではなく、会社試験で追加serverを不要にするfile-backed保存です。

## 動作確認

```powershell
Push-Location modules/knowledge
uv run --extra dev --extra semantica pytest
Pop-Location

uv run --extra dev --extra semantica pytest `
  tests/test_semantica_closed_loop_e2e.py
```

## 次に読む

- [はじめてのNEXUS SEED](getting-started.ja.md)
- [困ったときの確認項目](troubleshooting.ja.md)
