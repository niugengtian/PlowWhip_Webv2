HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PlowWhip V1</title>
  <style>
    :root { color-scheme: light; --ink:#17221b; --muted:#667269; --line:#dbe2dc; --paper:#f6f7f2; --card:#fff; --accent:#246b45; --warn:#9a5b16; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif; }
    header { position:sticky; top:0; z-index:2; display:flex; align-items:center; gap:24px; padding:14px clamp(18px,4vw,56px); border-bottom:1px solid var(--line); background:rgba(246,247,242,.96); }
    .brand { font-weight:760; letter-spacing:-.03em; font-size:20px; }
    nav { display:flex; gap:6px; flex-wrap:wrap; }
    button, input, textarea { font:inherit; }
    button { border:1px solid var(--line); border-radius:9px; background:var(--card); color:var(--ink); padding:8px 12px; cursor:pointer; }
    button:hover, button:focus-visible { border-color:var(--accent); outline:none; }
    nav button[aria-current="page"], .primary { color:#fff; border-color:var(--accent); background:var(--accent); }
    main { max-width:1120px; margin:auto; padding:clamp(24px,5vw,64px) clamp(18px,4vw,56px); }
    section[hidden] { display:none; }
    h1 { margin:0 0 8px; font-size:clamp(30px,5vw,54px); line-height:1.04; letter-spacing:-.045em; }
    h2 { margin:0 0 14px; font-size:20px; letter-spacing:-.02em; }
    p { color:var(--muted); }
    .lede { max-width:640px; font-size:17px; margin:0 0 30px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
    .card { min-width:0; padding:20px; border:1px solid var(--line); border-radius:14px; background:var(--card); box-shadow:0 1px 1px rgba(23,34,27,.03); }
    .wide { grid-column:1/-1; }
    .row { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 0; border-top:1px solid var(--line); }
    .row:first-child { border-top:0; }
    .row button { text-align:left; }
    .status { display:inline-block; border-radius:999px; background:#edf2ee; padding:3px 9px; color:var(--accent); font-size:12px; font-weight:700; }
    form { display:grid; gap:10px; }
    label { font-weight:650; }
    input, textarea { width:100%; border:1px solid var(--line); border-radius:9px; background:#fff; padding:10px 12px; color:var(--ink); }
    textarea { min-height:92px; resize:vertical; }
    pre { margin:0; max-height:360px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; border-radius:9px; background:#f2f4ef; padding:12px; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; }
    .muted { color:var(--muted); }
    .notice { min-height:24px; color:var(--warn); }
    @media (max-width:720px) { header { align-items:flex-start; flex-direction:column; gap:10px; } .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <div class="brand">PlowWhip V1</div>
    <nav aria-label="主要区域">
      <button data-view="home" aria-current="page">全局首页</button>
      <button data-view="project">项目详情</button>
      <button data-view="task">Task 详情</button>
      <button data-view="settings">设置与资源库</button>
    </nav>
  </header>
  <main>
    <section id="home">
      <h1>一条真实主线。</h1>
      <p class="lede">消息进入 SQLite，Cronner 每次只推进一个动作，Evidence 决定 Done 或 NeedsDecision。</p>
      <div class="grid">
        <div class="card">
          <h2>提交消息</h2>
          <form id="message-form">
            <label for="project-id">项目 ID</label><input id="project-id" required pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}">
            <label for="message">指令</label><textarea id="message" required placeholder="写入 result.txt: 闭环完成"></textarea>
            <button class="primary" type="submit">进入队列</button>
          </form>
          <div class="notice" id="home-notice" role="status"></div>
        </div>
        <div class="card">
          <h2>项目</h2><div id="project-list" class="muted">正在读取…</div>
        </div>
        <div class="card wide">
          <h2>跨项目精确搜索</h2>
          <form id="search-form"><label for="search">Task / Goal / Artifact 索引</label><input id="search" required maxlength="128"><button type="submit">搜索</button></form>
          <pre id="search-results">不调用模型。</pre>
        </div>
      </div>
    </section>

    <section id="project" hidden>
      <h1>项目详情</h1><p class="lede">当前 Task、四态、内部 phase 与最新 Evidence。</p>
      <div class="grid" id="project-detail"><div class="card">请从全局首页选择项目。</div></div>
    </section>

    <section id="task" hidden>
      <h1>Task 详情</h1><p class="lede">执行角色与 Checker 相互独立；这里只展示权威事实和有界输出。</p>
      <div class="grid">
        <div class="card wide"><h2>状态</h2><pre id="task-state">请先选择 Task。</pre></div>
        <div class="card"><h2>角色 / Provider / Generation</h2><div id="task-sessions" class="muted">—</div></div>
        <div class="card"><h2>最后 20 行</h2><pre id="task-output">—</pre></div>
        <div class="card"><h2>Artifact / Evidence / Handoff</h2><pre id="task-evidence">—</pre></div>
        <div class="card">
          <h2>明确操作</h2>
          <div class="actions"><button id="cancel-task">取消</button><button id="rerun-task">重新执行</button></div>
          <form id="decision-form"><label for="decision">提供决定</label><textarea id="decision" placeholder="写入 result.txt: 修订内容"></textarea><button type="submit">提交决定</button></form>
          <form id="plan-form"><label for="plan">更新计划（JSON）</label><textarea id="plan" placeholder='{"alternatives":[],"selected":0,"tasks":[]}'></textarea><button type="submit">提交计划</button></form>
          <div class="notice" id="task-notice" role="status"></div>
        </div>
      </div>
    </section>

    <section id="settings" hidden>
      <h1>设置与资源库</h1><p class="lede">正文以文件为真源；TaskSession 创建时冻结实际 SHA 与生效设置来源。</p>
      <div class="grid"><div class="card"><h2>设置</h2><pre id="settings-data">正在读取…</pre></div><div class="card"><h2>资源库</h2><pre id="library-data">正在读取…</pre></div></div>
    </section>
  </main>
  <script>
    const views=[...document.querySelectorAll('main>section')];
    const nav=[...document.querySelectorAll('nav button')];
    let currentProject=null,currentTask=null;
    const key=()=>globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random()}`;
    const show=name=>{views.forEach(v=>v.hidden=v.id!==name);nav.forEach(b=>b.setAttribute('aria-current',b.dataset.view===name?'page':'false'));if(name==='settings')loadSettings();};
    nav.forEach(button=>button.addEventListener('click',()=>show(button.dataset.view)));
    async function api(path,options){const response=await fetch(path,options);const body=await response.json();if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);return body;}
    const pretty=value=>JSON.stringify(value,null,2);
    async function loadProjects(){
      const box=document.querySelector('#project-list');box.textContent='';
      try { const data=await api('/api/projects'); if(!data.projects.length){box.textContent='还没有项目。';return;}
        data.projects.forEach(project=>{const row=document.createElement('div');row.className='row';const button=document.createElement('button');button.textContent=project.project_id;button.addEventListener('click',()=>openProject(project.project_id));const status=document.createElement('span');status.className='status';status.textContent=project.outcome||project.public_status||'pending';row.append(button,status);box.append(row);});
      } catch(error){box.textContent=error.message;}
    }
    async function openProject(id){
      currentProject=id;show('project');const box=document.querySelector('#project-detail');box.textContent='正在读取…';
      try { const data=await api(`/api/projects/${encodeURIComponent(id)}`);box.textContent='';const state=document.createElement('div');state.className='card wide';const title=document.createElement('h2');title.textContent=id;const pre=document.createElement('pre');pre.textContent=pretty(data.task||{});state.append(title,pre);box.append(state);if(data.task){const button=document.createElement('button');button.className='primary';button.textContent='查看 Task 证据';button.addEventListener('click',()=>openTask(data.task.id));state.append(button);}}
      catch(error){box.textContent=error.message;}
    }
    async function openTask(id){
      currentTask=id;show('task');
      try { const data=await api(`/api/tasks/${encodeURIComponent(id)}`);document.querySelector('#task-state').textContent=pretty(data.task);document.querySelector('#task-output').textContent=(data.last_output||[]).join('\n')||'无输出';document.querySelector('#task-evidence').textContent=pretty({artifacts:data.artifacts,handoffs:data.handoffs});const box=document.querySelector('#task-sessions');box.textContent='';(data.sessions||[]).forEach(session=>{const pre=document.createElement('pre');pre.textContent=pretty({role:session.role_key,provider:session.provider_key,model:session.model,generation:session.generation,status:session.status,usage:(data.model_usage||[]).find(x=>x.task_session_id===session.task_session_id)?.normalized_total||0});box.append(pre);});}
      catch(error){document.querySelector('#task-notice').textContent=error.message;}
    }
    async function postAction(kind,instruction='',plan){if(!currentTask||!currentProject)throw new Error('请先选择项目和 Task');return api('/api/actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:currentProject,task_id:currentTask,kind,instruction,plan,idempotency_key:key()})});}
    document.querySelector('#message-form').addEventListener('submit',async event=>{event.preventDefault();const notice=document.querySelector('#home-notice');try{await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:document.querySelector('#project-id').value,content:document.querySelector('#message').value,idempotency_key:key()})});notice.textContent='已进入 SQLite 队列。';await loadProjects();}catch(error){notice.textContent=error.message;}});
    document.querySelector('#search-form').addEventListener('submit',async event=>{event.preventDefault();const output=document.querySelector('#search-results');try{output.textContent=pretty(await api(`/api/search?q=${encodeURIComponent(document.querySelector('#search').value)}`));}catch(error){output.textContent=error.message;}});
    document.querySelector('#cancel-task').addEventListener('click',async()=>{try{await postAction('cancel');document.querySelector('#task-notice').textContent='取消已入队。';}catch(error){document.querySelector('#task-notice').textContent=error.message;}});
    document.querySelector('#rerun-task').addEventListener('click',async()=>{try{await postAction('rerun');document.querySelector('#task-notice').textContent='重新执行已入队。';}catch(error){document.querySelector('#task-notice').textContent=error.message;}});
    document.querySelector('#decision-form').addEventListener('submit',async event=>{event.preventDefault();try{await postAction('provide_decision',document.querySelector('#decision').value);document.querySelector('#task-notice').textContent='决定已入队。';}catch(error){document.querySelector('#task-notice').textContent=error.message;}});
    document.querySelector('#plan-form').addEventListener('submit',async event=>{event.preventDefault();try{await postAction('provide_plan','',JSON.parse(document.querySelector('#plan').value));document.querySelector('#task-notice').textContent='计划已入队。';}catch(error){document.querySelector('#task-notice').textContent=error.message;}});
    async function loadSettings(){try{const data=await api('/api/settings-library');document.querySelector('#settings-data').textContent=pretty(data.settings.map(({setting_key,value,source,scope,project_id})=>({setting_key,value,source,scope,project_id})));document.querySelector('#library-data').textContent=pretty(data.library.map(({kind,item_key,revision,sha256})=>({kind,item_key,revision,sha256})));}catch(error){document.querySelector('#settings-data').textContent=error.message;}}
    loadProjects();
  </script>
</body>
</html>
"""
