"""Dependency-free static assets for the Cockpit single-page interface."""

INDEX_HTML = r'''<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>NEXUS SEED Cockpit</title>
  <link rel="stylesheet" href="/cockpit/styles.css">
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">N</span><div><strong>NEXUS SEED</strong><small>HUMAN COCKPIT</small></div></div>
      <nav id="navigation" aria-label="Cockpit navigation">
        <button class="nav-item active" data-view="overview"><span>◫</span>Overview</button>
        <button class="nav-item" data-view="being"><span>◎</span>Being</button>
        <button class="nav-item" data-view="activity"><span>↳</span>Activity</button>
        <button class="nav-item" data-view="work"><span>◇</span>Work</button>
        <button class="nav-item" data-view="reviews"><span>!</span>Reviews <b id="review-nav-count"></b></button>
        <button class="nav-item" data-view="providers"><span>⌘</span>Providers</button>
        <button class="nav-item" data-view="system"><span>⋯</span>System</button>
      </nav>
      <div class="sidebar-foot">
        <div class="live-dot"><i></i><span id="sidebar-runtime">CONNECTING</span></div>
        <small id="last-update">Waiting for snapshot</small>
      </div>
    </aside>
    <section class="workspace">
      <header class="topbar">
        <div><p class="eyebrow" id="view-eyebrow">CURRENT SITUATION</p><h1 id="view-title">Overview</h1></div>
        <div class="top-actions">
          <div class="health-pill"><i></i><span id="runtime-status">Connecting</span></div>
          <button class="button primary" id="new-goal">＋ New Goal</button>
          <button class="icon-button" id="refresh" title="Refresh">↻</button>
        </div>
      </header>
      <main id="app" aria-live="polite"><div class="loading"><span></span><p>Compiling current context…</p></div></main>
    </section>
  </div>

  <dialog id="auth-dialog">
    <form method="dialog" id="auth-form" class="dialog-card">
      <p class="eyebrow">SECURE CONNECTION</p><h2>Cockpitへ接続</h2>
      <p>NEXUS_SEED_WEBHOOK_TOKENを入力してください。トークンはこのタブ内だけに保持されます。</p>
      <label>Access token<input id="auth-token" type="password" autocomplete="off" required></label>
      <div class="dialog-actions"><button class="button primary" value="default">Connect</button></div>
      <p class="form-error" id="auth-error"></p>
    </form>
  </dialog>
  <div id="toast" role="status"></div>
  <script src="/cockpit/app.js" defer></script>
</body>
</html>'''


STYLES_CSS = r'''
:root{--bg:#071019;--panel:#0c1722;--panel-2:#101e2b;--line:#20303d;--text:#e8f0f4;--muted:#8497a6;--accent:#55e6b5;--accent-2:#6da8ff;--warn:#f3bf62;--danger:#ff7f7f;--ok:#64e1a9;--radius:16px;--shadow:0 20px 60px rgba(0,0,0,.25)}
.assistance-card{padding:20px;border:1px solid #745b35;border-radius:16px;background:linear-gradient(145deg,#211b13,#101a22);box-shadow:var(--shadow)}.assistance-card h3{margin:5px 0 8px;font-size:17px}.assistance-purpose{margin:0 0 15px;color:#dce6eb;font-size:13px}.assistance-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin:14px 0}.assistance-fact{padding:12px;border:1px solid #2b3943;border-radius:10px;background:#0b151e}.assistance-fact strong{display:block;margin-bottom:6px;color:#8da1ae;font-size:9px;letter-spacing:.1em;text-transform:uppercase}.assistance-fact p{margin:0;color:#c2cdd4;font-size:11px;line-height:1.55}.assistance-card .next-action{padding:12px 14px;border-left:3px solid var(--warn);background:#241e15;color:#efd8aa;font-size:12px;line-height:1.55}.attempt-list{margin:0;padding-left:18px;color:#aab9c2;font-size:11px;line-height:1.65}@media(max-width:760px){.assistance-grid{grid-template-columns:1fr}}
'''


APP_JS = r'''
const state={snapshot:null,view:"overview",token:sessionStorage.getItem("nexus-cockpit-token")||""};
const $=s=>document.querySelector(s);const app=$("#app");
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const raw=v=>`<details><summary>Raw facts / audit detail</summary><pre class="raw">${esc(JSON.stringify(v,null,2))}</pre></details>`;
const when=v=>v?new Intl.DateTimeFormat("ja-JP",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}).format(new Date(v)):"—";
const badge=v=>`<span class="badge ${esc(v)}">${esc(v||"UNKNOWN")}</span>`;
const empty=(title,text="")=>`<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
const section=(title,sub,body)=>`<section class="section"><div class="section-head"><div><h2>${esc(title)}</h2><p>${esc(sub||"")}</p></div></div>${body}</section>`;
function headers(){return state.token?{"Authorization":`Bearer ${state.token}`}:{}}
async function load(){try{const res=await fetch("/cockpit/api/snapshot",{headers:headers(),cache:"no-store"});if(res.status===401){showAuth();return}if(!res.ok)throw new Error(`Cockpit API ${res.status}`);state.snapshot=await res.json();updateChrome();render()}catch(err){app.innerHTML=empty("Cockpit snapshotを取得できません",err.message);$("#runtime-status").textContent="Offline";$("#sidebar-runtime").textContent="OFFLINE"}}
function showAuth(){const d=$("#auth-dialog");if(!d.open)d.showModal();$("#auth-error").textContent=""}
$("#auth-form").addEventListener("submit",e=>{e.preventDefault();state.token=$("#auth-token").value.trim();sessionStorage.setItem("nexus-cockpit-token",state.token);$("#auth-dialog").close();load()});
function updateChrome(){const s=state.snapshot;$("#runtime-status").textContent=s.overview.runtime.status;$("#sidebar-runtime").textContent=`RUNTIME ${s.overview.runtime.status}`;$("#last-update").textContent=`Updated ${when(s.generated_at)}`;$("#review-nav-count").textContent=s.reviews.length||"";$("#new-goal").style.display=s.control.enabled?"":"none"}
const titles={overview:["CURRENT SITUATION","Overview"],being:["PERSISTENT BEING","Being"],activity:["CAUSAL HISTORY","Activity"],work:["DURABLE REQUIREMENTS","Work"],reviews:["HUMAN DECISIONS","Reviews"],providers:["EXECUTION FEDERATION","Providers"],system:["RUNTIME HEALTH","System"]};
document.querySelectorAll(".nav-item").forEach(b=>b.addEventListener("click",()=>{state.view=b.dataset.view;document.querySelectorAll(".nav-item").forEach(x=>x.classList.toggle("active",x===b));const t=titles[state.view];$("#view-eyebrow").textContent=t[0];$("#view-title").textContent=t[1];render()}));$("#refresh").onclick=load;
function metric(label,value,sub="",tone=""){return `<div class="metric"><div class="label"><span>${esc(label)}</span></div><strong class="${tone}">${esc(value)}</strong><small>${esc(sub)}</small></div>`}
function render(){if(!state.snapshot)return;({overview:renderOverview,being:renderBeing,activity:renderActivity,work:renderWork,reviews:renderReviews,providers:renderProviders,system:renderSystem}[state.view]||renderOverview)()}
function attention(items){if(!items.length)return empty("判断を必要とする項目はありません","Runtimeは次のEventを待っています。");return `<div class="attention-list">${items.map(x=>x.kind==="capability_assistance"?capabilityAssistanceCard(x.assistance,true):`<article class="attention-item ${x.severity}"><span class="signal"></span><div><h3>${esc(x.title)}</h3><p>${esc(x.message)}</p>${raw(x.raw)}</div><div class="actions">${attentionActions(x)}</div></article>`).join("")}</div>`}
function attentionActions(x){if(!state.snapshot.control.enabled)return"";if(["review","capability_acquisition"].includes(x.kind))return `<button class="button small primary" onclick="review('${esc(x.target_id)}','approve')">Approve</button><button class="button small" onclick="review('${esc(x.target_id)}','reject')">Reject</button>`;if(x.kind==="self_question")return `<button class="button small primary" onclick="answerQuestion('${esc(x.target_id)}')">Answer</button>`;if(x.kind==="blocked_work")return `<button class="button small" onclick="workCommand('resume','${esc(x.target_id)}')">Resume</button>`;return""}
function goalCard(g){return `<article class="card"><div class="card-top"><div><h3>${esc(g.title)}</h3><p>${esc(g.objective)}</p></div>${badge(g.status)}</div><div class="meta"><span>Priority ${esc(g.priority)}</span><span>${when(g.updated_at)}</span></div>${state.snapshot.control.enabled?`<div class="actions"><button class="button small" onclick="goalCommand('pause','${g.id}')">Pause</button><button class="button small" onclick="goalCommand('resume','${g.id}')">Resume</button><button class="button small danger" onclick="goalCommand('cancel','${g.id}')">Cancel</button></div>`:""}</article>`}
function intentionCard(i){return `<article class="card"><div class="card-top"><div><p class="eyebrow">${esc(i.goal_title||"GOAL")}</p><h3>${esc(i.focus)}</h3></div>${badge(i.status)}</div><p>${esc(i.reason||"No additional reason recorded")}</p><div class="meta"><span>Priority ${esc(i.priority||"—")}</span><span>Updated ${when(i.updated_at)}</span></div><details><summary>Reconsideration conditions</summary><pre class="raw">${esc(JSON.stringify(i.reconsider_on,null,2))}</pre></details></article>`}
function activityCard(a){return `<article class="activity-card"><span class="result">${badge(a.result.replace(" ","_"))}</span><h3>${esc(a.title)}</h3><p class="eyebrow">${when(a.updated_at)}</p><div class="activity-steps">${a.steps.map(s=>`<span>${esc(s)}</span>`).join("")}</div>${raw(a.details)}</article>`}
function renderOverview(){const s=state.snapshot,o=s.overview,c=o.counts,ordinary=s.needs_attention.filter(x=>x.kind!=="capability_assistance");app.innerHTML=`<div class="grid metrics">${metric("Runtime",o.runtime.status,"Durable runtime")}${metric("LLM",o.llm.status,o.llm.model||"Not configured")}${metric("Goals",c.active_goals,"Active")}${metric("Intentions",c.active_intentions,"Non-terminal")}${metric("Reviews",c.pending_reviews,"Waiting for human",c.pending_reviews?"tone-warning":"")}${metric("Errors / Warnings",`${c.errors} / ${c.warnings}`,"Needs attention",c.errors?"tone-error":c.warnings?"tone-warning":"")}</div><section class="focus-card"><p class="eyebrow">CURRENT FOCUS · ${esc(o.focus.disposition)}</p><h2>${esc(o.focus.label)}</h2><div class="focus-meta"><span>${esc(o.focus.source_event_type||"Persistent context")}</span><span>•</span><span>${when(o.focus.updated_at)}</span></div></section>${s.capability_assistance.length?section("Human Assistance",`${s.capability_assistance.length}件のCapability不足で判断または提供が必要です`,`<div class="stack">${s.capability_assistance.map(x=>capabilityAssistanceCard(x)).join("")}</div>`):""}<div class="grid two-col"><div>${section("Needs Attention","Human decision and blocked conditions",attention(ordinary))}</div><div>${section("Goals / Intentions","What NEXUS SEED is pursuing",`<div class="stack">${s.goals.filter(g=>g.status==="ACTIVE").slice(0,4).map(goalCard).join("")||empty("Active Goalなし")}${s.intentions.filter(i=>!["SATISFIED","ABANDONED"].includes(i.status)).slice(0,3).map(intentionCard).join("")}</div>`)}</div></div>${section("Recent Activity","Events grouped into human-readable causal activity",`<div class="timeline">${s.activities.slice(0,6).map(activityCard).join("")||empty("Activityはまだありません")}</div>`)}`}
function listBlock(items,none="None"){return items?.length?`<ul class="list">${items.map(x=>`<li>${esc(typeof x==="string"?x:JSON.stringify(x))}</li>`).join("")}</ul>`:empty(none)}
function renderBeing(){const b=state.snapshot.being,self=b.self,master=b.master;const claims=Object.entries(master.claims||{}).flatMap(([category,items])=>items.map(x=>({...x,category})));app.innerHTML=`<div class="grid being-hero"><article class="panel panel-pad"><p class="eyebrow">SELF PROJECTION</p><div class="identity">${esc(typeof self.identity==="string"?self.identity:JSON.stringify(self.identity)||"Identity not recorded")}</div><h3>Current concerns</h3>${listBlock(self.current_concerns,"Current concernなし")}<h3>Commitments</h3>${listBlock(self.commitments,"Commitmentなし")}<h3>Recent reflection</h3>${self.recent_reflection?raw(self.recent_reflection):empty("Reflectionはまだありません")}</article><article class="panel panel-pad"><p class="eyebrow">MASTER · ${esc(master.master_id)}</p><h3>Claims with epistemic status</h3>${claims.length?claims.map(c=>`<div class="claim"><div class="card-top"><strong>${esc(c.category)} · ${esc(c.key)}</strong>${badge(c.status)}</div><div class="claim-value">${esc(typeof c.value==="string"?c.value:JSON.stringify(c.value))}</div></div>`).join(""):empty("Master claimはまだありません","OBSERVED / INFERRED / CONFIRMEDを区別して表示します。")}</article></div><div class="grid two-col">${section("Unresolved Self Questions","Questions that may require the Master",self.unresolved_questions.length?`<div class="stack">${self.unresolved_questions.map(q=>`<article class="card"><h3>${esc(q.text)}</h3>${state.snapshot.control.enabled?`<div class="actions"><button class="button small primary" onclick="answerQuestion('${q.id}')">Answer</button></div>`:""}${raw(q.raw)}</article>`).join("")}</div>`:empty("未回答の質問はありません"))}${section("Available Capabilities","Projected live from Capability Registry",`<div class="panel panel-pad"><div class="capabilities">${self.capabilities.map(x=>`<span class="badge">${esc(x)}</span>`).join("")||"None registered"}</div></div>`)}</div>${section("Persistent Intentions","Long-lived state beneath existing Goals",`<div class="card-grid">${state.snapshot.intentions.map(intentionCard).join("")||empty("Intentionはまだありません")}</div>`)}`}
function renderActivity(){app.innerHTML=section("Activity Timeline","Process names are hidden until you open internal details",`<div class="timeline">${state.snapshot.activities.map(activityCard).join("")||empty("Activityはまだありません")}</div>`)}
function workCard(w){return `<article class="card"><div class="card-top"><div><p class="eyebrow">${esc(w.type)}</p><h3>${esc(w.objective||w.type)}</h3></div>${badge(w.status)}</div><p>${esc(w.reason||"")}</p><div class="meta"><span>Priority ${esc(w.priority)}</span><span>${when(w.updated_at)}</span></div>${w.missing_capabilities?.length?`<div class="capabilities">${w.missing_capabilities.map(x=>`<span class="badge error">${esc(x)}</span>`).join("")}</div>`:""}${state.snapshot.control.enabled&&!['SATISFIED','CANCELLED'].includes(w.status)?`<div class="actions"><button class="button small" onclick="workCommand('pause','${w.id}')">Pause</button><button class="button small" onclick="workCommand('resume','${w.id}')">Resume</button><button class="button small" onclick="changePriority('${w.id}')">Priority</button><button class="button small" onclick="changeProvider('${w.id}')">Provider</button><button class="button small danger" onclick="workCommand('cancel','${w.id}')">Cancel</button></div>`:""}${raw(w)}</article>`}
function capabilityAssistanceCard(a,compact=false){const goal=a.goal?`${a.goal.title} · ${a.goal.objective}`:"Goal未関連",intention=a.intention?`${a.intention.focus} (${a.intention.status})`:"Intention未記録",attempts=a.automatic_acquisition.tried.length?`<ul class="attempt-list">${a.automatic_acquisition.tried.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`:"<p>自動取得経路は見つかりませんでした。</p>";const reviewButtons=state.snapshot.control.enabled?a.review_ids.map(id=>`<button class="button small primary" onclick="review('${esc(id)}','approve')">Approve</button><button class="button small danger" onclick="review('${esc(id)}','reject')">Reject</button>`).join(""):"";return `<article class="assistance-card"><p class="eyebrow">HUMAN ASSISTANCE · ${esc(a.work_count)} WORK BLOCKED</p><h3>能力が足りないため進められません</h3><p class="assistance-purpose"><strong>目的:</strong> ${esc(a.purpose)}</p><div class="capabilities">${a.missing_capabilities.map(x=>`<span class="badge error">${esc(x)}</span>`).join("")}</div>${compact?"":`<div class="assistance-grid"><div class="assistance-fact"><strong>Goal / Intention</strong><p>${esc(goal)}<br>${esc(intention)}</p></div><div class="assistance-fact"><strong>自動解決で試したこと</strong>${attempts}</div><div class="assistance-fact"><strong>現在進めない理由</strong><p>${esc(a.automatic_acquisition.reason)}</p></div><div class="assistance-fact"><strong>影響</strong><p>${esc(a.work_count)} Work blocked</p></div></div>`}<div class="next-action"><strong>必要な対応:</strong> ${esc(a.human_action.summary)}</div><div class="actions">${reviewButtons}${a.review_ids.length?`<button class="button small" onclick="openReviews()">Review一覧</button>`:""}${state.snapshot.control.enabled?`<button class="button small" onclick='deferAssistance(${JSON.stringify(a.work_ids)})'>今回は保留</button>`:""}</div>${raw(a.raw_trace)}</article>`}
function renderWork(){const w=state.snapshot.work,a=state.snapshot.capability_assistance;app.innerHTML=`${a.length?section("Capability Assistance","自動取得で解決できず、人間の対応が必要な項目",`<div class="stack">${a.map(x=>capabilityAssistanceCard(x)).join("")}</div>`):""}${section("Active Work",`${w.active.length} durable requirements`,`<div class="card-grid">${w.active.map(workCard).join("")||empty("Active Workなし")}</div>`)}${section("Blocked Work",`${w.blocked.length} items need capability, provider, or planning recovery`,`<div class="card-grid">${w.blocked.map(workCard).join("")||empty("Blocked Workなし")}</div>`)}${section("Completed",`Most recent ${w.completed.length}`,`<div class="card-grid">${w.completed.map(workCard).join("")||empty("Completed Workなし")}</div>`)}`}
function reviewCard(r){return `<article class="card"><div class="card-top"><div><p class="eyebrow">${esc(r.category)}</p><h3>${esc(r.summary)}</h3></div>${badge("REVIEW")}</div><p>${esc(r.process||r.event_type)}</p><div class="meta"><span>${when(r.created_at)}</span><span>${esc(r.id)}</span></div>${state.snapshot.control.enabled?`<div class="actions"><button class="button small primary" onclick="review('${r.id}','approve')">Approve</button><button class="button small danger" onclick="review('${r.id}','reject')">Reject</button></div>`:""}${raw(r)}</article>`}
function renderReviews(){app.innerHTML=section("Pending Reviews","All decisions go through the existing authorized Control Plane",`<div class="card-grid">${state.snapshot.reviews.map(reviewCard).join("")||empty("レビュー待ちはありません","NEXUS SEEDは人間の判断を待っていません。")}</div>`)}
function renderProviders(){app.innerHTML=section("Execution Providers","Administrative status and observed health remain distinct",`<div class="card-grid">${state.snapshot.providers.map(p=>`<article class="card"><div class="card-top"><div><p class="eyebrow">${esc(p.kind)}</p><h3>${esc(p.name)} <small>v${esc(p.version)}</small></h3></div><span class="provider-health ${p.operational?'':'bad'}"></span></div><div class="meta">${badge(p.status)}${badge(p.health)}<span>Trust ${esc(p.trust_level)}</span></div>${raw(p)}</article>`).join("")||empty("Providerはまだ登録されていません")}</div>`)}
function renderSystem(){const s=state.snapshot;app.innerHTML=`<div class="grid metrics">${metric("Runtime",s.overview.runtime.status,"Request reached live runtime")}${metric("Phase 6",s.overview.phase6.enabled?"ON":"OFF","Feature flag")}${metric("LLM",s.overview.llm.status,s.overview.llm.model||"")}${metric("Raw trace","AVAILABLE","Audit history retained")}</div>${section("Delivery Health","Durable Event delivery",`<article class="panel panel-pad">${raw(s.system.delivery)}</article>`)}${section("Current Status Counts","Read-only projections",`<div class="grid two-col"><article class="panel panel-pad"><h3>Processes</h3>${raw(s.system.process_counts)}</article><article class="panel panel-pad"><h3>Work</h3>${raw(s.system.work_counts)}</article></div>`)}${section("Recent Failures","Human explanation appears in Needs Attention; raw facts stay here",`<div class="card-grid">${s.system.recent_failures.map(x=>`<article class="card"><h3>${esc(x.definition)}</h3>${badge(x.status)}${raw(x)}</article>`).join("")||empty("Current unresolved failureなし")}</div>`)}`}
async function sendCommand(command,quiet=false){try{const res=await fetch("/control",{method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({command,source_channel:"cockpit",source_message_id:crypto.randomUUID(),idempotency_key:`cockpit:${crypto.randomUUID()}`})});if(res.status===401){showAuth();return false}const out=await res.json();if(!res.ok||out.status!=="EXECUTED")throw new Error(out.failure_reason||out.error||out.message||"Command rejected");if(!quiet)toast(`${out.message||"Command executed"} · 更新ボタンで最新状態を取得できます`);return true}catch(err){if(!quiet)toast(err.message,true);return false}}
window.review=(id,decision)=>sendCommand(`/${decision} ${id}`);window.goalCommand=(action,id)=>sendCommand(`/goal ${action} ${id}`);window.workCommand=(action,id)=>sendCommand(`/${action} ${id}`);
window.openReviews=()=>{const button=document.querySelector('[data-view="reviews"]');if(button)button.click()};
window.deferAssistance=async ids=>{let succeeded=0;for(const id of ids){if(await sendCommand(`/pause ${id}`,true))succeeded++}toast(succeeded===ids.length?`${succeeded}件のWorkをControl Plane経由で保留しました · 更新ボタンで反映できます`:`${succeeded}/${ids.length}件を保留しました`,succeeded!==ids.length)};
window.answerQuestion=id=>{const answer=prompt("この質問への回答を入力してください");if(answer?.trim())sendCommand(`/answer ${id} answer=${JSON.stringify(answer.trim())}`)};
window.changePriority=id=>{const value=prompt("Priority: LOW / NORMAL / HIGH / CRITICAL","HIGH");if(value)sendCommand(`/priority ${id} ${value.toUpperCase()}`)};
window.changeProvider=id=>{const provider=prompt("Provider name or id");if(provider)sendCommand(`/provider ${id} PREFER ${JSON.stringify(provider)}`)};
$("#new-goal").onclick=()=>{const objective=prompt("Goal objective");if(!objective?.trim())return;const title=prompt("Goal title",objective.trim().slice(0,60))||objective.trim();sendCommand(`/goal create title=${JSON.stringify(title)} objective=${JSON.stringify(objective.trim())} priority=NORMAL`)};
function toast(message,error=false){const t=$("#toast");t.textContent=message;t.className=error?"show error":"show";setTimeout(()=>t.className="",3200)}
load();
'''


__all__ = ["APP_JS", "INDEX_HTML", "STYLES_CSS"]
