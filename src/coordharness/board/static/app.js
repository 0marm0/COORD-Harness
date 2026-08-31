const formatCount=value=>Number(value||0).toLocaleString();
const esc=value=>String(value??"").replace(/[&<>"']/g,char=>({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[char]));
const empty=()=>document.querySelector("#empty").content.cloneNode(true);
const badge=status=>`<span class="badge ${esc(status)}">${esc(status||"unknown")}</span>`;
function cards(rows,render){
  if(!rows.length)return empty();
  const element=document.createElement("div");
  element.className="grid";
  element.innerHTML=rows.map(render).join("");
  return element;
}
const graphValues=graph=>({
  nodes:graph&&Array.isArray(graph.nodes)?graph.nodes:[],
  edges:graph&&Array.isArray(graph.edges)?graph.edges:[],
});
const stableCompare=(left,right)=>left<right?-1:left>right?1:0;
const graphEdgeKey=edge=>[
  edge.id,edge.source,edge.target,edge.kind,edge.relationship_state,edge.source_field,
].map(value=>String(value??"")).join("\u0000");
function normalizeGraph(graph){
  const values=graphValues(graph);
  const nodesById=new Map();
  for(const suppliedNode of values.nodes){
    const id=String(suppliedNode&&suppliedNode.id||"");
    if(!id||nodesById.has(id))continue;
    nodesById.set(id,{...suppliedNode,id});
  }
  const edges=values.edges
    .map(suppliedEdge=>({
      ...suppliedEdge,
      id:String(suppliedEdge&&suppliedEdge.id||""),
      source:String(suppliedEdge&&suppliedEdge.source||""),
      target:String(suppliedEdge&&suppliedEdge.target||""),
    }))
    .filter(edge=>edge.source&&edge.target)
    .sort((left,right)=>stableCompare(graphEdgeKey(left),graphEdgeKey(right)));
  for(const edge of edges){
    if(!nodesById.has(edge.source)){
      nodesById.set(edge.source,{
        id:edge.source,kind:"missing_node",label:`Missing node: ${edge.source}`,missing:true,
      });
    }
    if(!nodesById.has(edge.target)){
      nodesById.set(edge.target,{
        id:edge.target,kind:"missing_node",label:`Missing target: ${edge.target}`,missing:true,
      });
    }
  }
  return {
    nodes:[...nodesById.values()].sort((left,right)=>stableCompare(left.id,right.id)),
    edges,
  };
}
function layoutGraph(graph){
  const normalized=normalizeGraph(graph);
  const ranks=new Map(normalized.nodes.map(node=>[node.id,0]));
  const indegrees=new Map(normalized.nodes.map(node=>[node.id,0]));
  const outgoing=new Map(normalized.nodes.map(node=>[node.id,[]]));
  for(const edge of normalized.edges){
    if(edge.source===edge.target)continue;
    outgoing.get(edge.source).push(edge.target);
    indegrees.set(edge.target,indegrees.get(edge.target)+1);
  }
  for(const targets of outgoing.values())targets.sort(stableCompare);
  const queue=normalized.nodes
    .filter(node=>indegrees.get(node.id)===0)
    .map(node=>node.id)
    .sort(stableCompare);
  while(queue.length){
    const source=queue.shift();
    for(const target of outgoing.get(source)){
      ranks.set(target,Math.max(ranks.get(target),ranks.get(source)+1));
      indegrees.set(target,indegrees.get(target)-1);
      if(indegrees.get(target)===0){
        queue.push(target);
        queue.sort(stableCompare);
      }
    }
  }
  // Cycles remain together in their deterministic fallback rank. Their paths
  // still show the exact supplied directions; the layout never invents edges.
  const columns=new Map();
  for(const node of normalized.nodes){
    const rank=ranks.get(node.id);
    if(!columns.has(rank))columns.set(rank,[]);
    columns.get(rank).push(node);
  }
  for(const nodes of columns.values())nodes.sort((left,right)=>stableCompare(left.id,right.id));
  // Order each rank by where its edge lands in the next one. Without this, a
  // column of forty children pointing at five parents draws forty crossing
  // lines and reads as noise; grouped, each parent's children sit together.
  const firstTargetOf=new Map();
  for(const edge of normalized.edges){
    if(edge.source===edge.target)continue;
    if(!firstTargetOf.has(edge.source))firstTargetOf.set(edge.source,edge.target);
  }
  const rankOrder=[...columns.entries()].sort((left,right)=>right[0]-left[0]);
  const indexWithin=new Map();
  for(const [,nodes] of rankOrder){
    nodes.sort((left,right)=>{
      const lt=indexWithin.get(firstTargetOf.get(left.id));
      const rt=indexWithin.get(firstTargetOf.get(right.id));
      if(lt!==rt)return (lt??Number.MAX_SAFE_INTEGER)-(rt??Number.MAX_SAFE_INTEGER);
      return stableCompare(left.id,right.id);
    });
    nodes.forEach((node,index)=>indexWithin.set(node.id,index));
  }
  const marginX=200;
  const marginY=60;
  const columnGap=520;
  // Tighten the vertical rhythm as a column grows, so a large board still fits.
  const busiest=Math.max(1,...[...columns.values()].map(nodes=>nodes.length));
  const rowGap=busiest>24?34:busiest>12?46:70;
  const maxRank=Math.max(0,...columns.keys());
  const maxRows=Math.max(1,...[...columns.values()].map(nodes=>nodes.length));
  const naturalWidth=marginX*2+maxRank*columnGap;
  const width=Math.max(1200,naturalWidth);
  const height=Math.max(220,marginY*2+(maxRows-1)*rowGap+60);
  const offsetX=(width-naturalWidth)/2;
  const positions=new Map();
  for(const [rank,nodes] of [...columns.entries()].sort((left,right)=>left[0]-right[0])){
    nodes.forEach((node,index)=>positions.set(node.id,{
      ...node,rank,x:offsetX+marginX+rank*columnGap,y:marginY+index*rowGap,
    }));
  }
  const nodes=normalized.nodes.map(node=>positions.get(node.id));
  const edges=normalized.edges.map(edge=>({
    ...edge,sourcePoint:positions.get(edge.source),targetPoint:positions.get(edge.target),
  }));
  return {width,height,nodes,edges,tight:rowGap<=46};
}
const graphCoordinate=value=>Number(value.toFixed(1));
function graphEdgePath(source,target,index){
  const radius=12;
  if(source.x===target.x&&source.y===target.y){
    return `M ${source.x+radius} ${source.y} C ${source.x+58} ${source.y-58}, ${source.x-58} ${source.y-58}, ${source.x} ${source.y-radius}`;
  }
  const deltaX=target.x-source.x;
  const deltaY=target.y-source.y;
  const length=Math.hypot(deltaX,deltaY);
  const startX=graphCoordinate(source.x+deltaX/length*radius);
  const startY=graphCoordinate(source.y+deltaY/length*radius);
  const endX=graphCoordinate(target.x-deltaX/length*radius);
  const endY=graphCoordinate(target.y-deltaY/length*radius);
  if(Math.abs(deltaX)<1){
    const bend=52+(index%4)*14;
    return `M ${startX} ${startY} C ${startX+bend} ${startY}, ${endX+bend} ${endY}, ${endX} ${endY}`;
  }
  const middleX=graphCoordinate((startX+endX)/2);
  return `M ${startX} ${startY} C ${middleX} ${startY}, ${middleX} ${endY}, ${endX} ${endY}`;
}
const graphNodeLabel=node=>String(node.label||node.id).slice(0,28);
function graphRowId(node){
  const supplied=String(node?.id||"");
  const candidate=node?.kind==="work"&&supplied.startsWith("work:")?supplied.slice(5):supplied;
  return (STATE.snapshot?.rows||[]).some(row=>row.id===candidate)?candidate:"";
}
function drawGraph(graph){
  const layout=layoutGraph(graph);
  if(!layout.nodes.length){
    return `<div class="card"><h3>Dependency graph</h3><div class="empty" role="status">No graph nodes or relationships.</div></div>`;
  }
  const missingNodes=layout.nodes.filter(node=>node.missing).length;
  const paths=layout.edges.map((edge,index)=>{
    const missing=edge.relationship_state==="missing_target"||
      edge.sourcePoint.missing||edge.targetPoint.missing;
    const description=`${edge.source} ${edge.kind||"relates to"} ${edge.target}; ${edge.relationship_state||"unknown state"}; derived from ${edge.source_field||"unspecified source"}`;
    return `<path class="gedge${missing?" missing":""}" d="${graphEdgePath(edge.sourcePoint,edge.targetPoint,index)}" data-edge-id="${esc(edge.id)}" data-source="${esc(edge.source)}" data-target="${esc(edge.target)}" marker-end="url(#gtip)" role="img" aria-label="${esc(description)}"><title>${esc(description)}</title></path>`;
  }).join("");
  const nodes=layout.nodes.map(node=>{
    const rowId=graphRowId(node);
    const classes=node.missing?"gnode missing":`gnode${rowId?" selectable":" graph-only"}${rowId&&STATE.selected===rowId?" sel":""}`;
    const description=`${node.missing?"Missing relationship endpoint node":rowId?"Selectable work graph node":"Graph-only node"}: ${node.label||node.id}; id ${node.id}; kind ${node.kind||"unknown"}`;
    return `<g class="${classes}" transform="translate(${node.x},${node.y})" data-node-id="${esc(node.id)}"${rowId?` data-row="${esc(rowId)}" tabindex="0" role="button"`:` data-graph-only="true" role="img"`} aria-label="${esc(description)}"><title>${esc(description)}</title><circle r="${layout.tight?5:10}"></circle>${layout.tight
      ? `<text x="-14" y="4" text-anchor="end">${esc(graphNodeLabel(node))}</text>`
      : `<text y="30" text-anchor="middle">${esc(graphNodeLabel(node))}</text><text y="45" text-anchor="middle" class="gkind">${esc(node.missing?"missing endpoint":node.kind||"")}</text>`}</g>`;
  }).join("");
  const sourceBound=layout.edges.filter(edge=>edge.relationship_state==="source_bound").length;
  return `<div class="card"><h3>Dependency graph</h3><p class="meta">${layout.nodes.length} nodes - ${layout.edges.length} edges - ${sourceBound} source-bound${missingNodes?` - ${missingNodes} missing endpoints`:""}.</p><div class="gwrap"><svg viewBox="0 0 ${layout.width} ${layout.height}" role="img" aria-labelledby="graph-svg-title graph-svg-description"><title id="graph-svg-title">Source-bound dependency graph</title><desc id="graph-svg-description">${layout.edges.length} directed relationships among ${layout.nodes.length} nodes. Arrows follow the supplied source-to-target direction. Dashed paths and hollow nodes mark relationships with a missing endpoint. Coordinates are deterministic and do not imply unrecorded relationships.</desc><defs><marker id="gtip" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z"></path></marker></defs>${paths}${nodes}</svg></div></div>`;
}
function renderGraph(graph){
  const values=graphValues(graph);
  const rows=values.edges.length?values.edges.map(edge=>`<tr><td>${esc(edge.source)}</td><td>${esc(edge.target)}</td><td>${esc(edge.kind)}</td><td class="${edge.relationship_state==="missing_target"?"missing":""}">${esc(edge.relationship_state)}</td><td class="meta">${esc(edge.source_field)}</td></tr>`).join(""):`<tr><td colspan="5" class="meta">No recorded relationships.</td></tr>`;
  return `${drawGraph(graph)}<div class="card"><h3>Edge provenance</h3><p class="meta">Every relationship below is rendered from the supplied edge endpoints. The source field records where the relationship came from.</p><div class="tablewrap"><table aria-label="Source-bound relationship edge provenance"><thead><tr><th>From</th><th>To</th><th>Kind</th><th>State</th><th>Derived from</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
// Owner marks. The same grid and the same palette the native apps draw, so a
// row looks like itself whichever client you happen to be reading it in.
// Drawn as SVG rather than shipped as images: nothing to fetch, nothing to
// license, and it stays crisp at the 18px these render at.
const AGENT_HEAD=["..#..#..",".######.","########","#.####.#","########","########",".######.","#.#..#.#"];
const MARK_INK={chat:"#ff6b33",codeTop:"#998cf2",codeBottom:"#4578ff",accelerated:"#f5c452",compute:"#33c759",operator:"#f5c452"};
function chatMark(id){
  const cells=AGENT_HEAD.flatMap((row,y)=>[...row].map((cell,x)=>cell==="#"?`<rect x="${x}" y="${y}" width="1" height="1"></rect>`:"").join(""));
  return `<svg class="mark" viewBox="0 0 8 8" role="img" aria-label="chat agent"><g fill="${MARK_INK.chat}">${cells.join("")}</g></svg>`;
}
function codeMark(id){
  return `<svg class="mark" viewBox="0 0 16 16" role="img" aria-label="code agent"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${MARK_INK.codeTop}"></stop><stop offset="1" stop-color="${MARK_INK.codeBottom}"></stop></linearGradient></defs><rect x="1" y="1" width="14" height="14" rx="4.2" fill="url(#${id})"></rect><path d="M5 4.8 L8 8 L5 11.2" fill="none" stroke="#fff" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"></path><path d="M9.4 11.2 H12.4" fill="none" stroke="#fff" stroke-width="1.4" stroke-linecap="round"></path></svg>`;
}
function siliconMark(accelerated){
  const ink=accelerated?MARK_INK.accelerated:MARK_INK.compute;
  const pins=[4.5,8,11.5].map(v=>`<path d="M${v} 3.5 V1.5 M${v} 12.5 V14.5 M3.5 ${v} H1.5 M12.5 ${v} H14.5"></path>`).join("");
  const core=accelerated
    ? `<rect x="3.5" y="3.5" width="9" height="9" rx="2" fill="${ink}"></rect><rect x="6" y="6" width="4" height="4" rx="1" fill="var(--ground,#0f1115)"></rect>`
    : `<rect x="3.5" y="3.5" width="9" height="9" rx="2" fill="none" stroke="${ink}" stroke-width="1.2"></rect><rect x="6" y="6" width="4" height="4" rx="1" fill="none" stroke="${ink}" stroke-width="1.2"></rect>`;
  return `<svg class="mark" viewBox="0 0 16 16" role="img" aria-label="${accelerated?"local accelerator":"local compute"}"><g stroke="${ink}" stroke-width="1.2" stroke-linecap="round" fill="none">${pins}</g>${core}</svg>`;
}
function operatorMark(){
  return `<svg class="mark" viewBox="0 0 16 16" role="img" aria-label="operator"><circle cx="8" cy="5.6" r="2.8" fill="${MARK_INK.operator}"></circle><path d="M2.6 14.4 a5.4 5.4 0 0 1 10.8 0 z" fill="${MARK_INK.operator}"></path></svg>`;
}
let markSeq=0;
// The marks are served as images, with the drawn versions behind them: if a
// file is missing the row still gets a mark rather than a broken-image icon.
function markImage(file,label,fallback){
  const id=`mk${markSeq++}`;
  queueMicrotask(()=>{
    const img=document.getElementById(id);
    if(img)img.addEventListener("error",()=>{img.outerHTML=fallback;},{once:true});
  });
  return `<img id="${id}" class="mark" src="/static/${file}" alt="${label}">`;
}
// The lane is the part before the colon: `claude:frontend` is the frontend
// session of the chat lane. An unrecognised lane gets no mark rather than a
// guessed one -- a wrong badge asserts something the board does not know.
function ownerMark(owner){
  const lane=String(owner||"").split(":")[0].trim().toLowerCase();
  const detail=String(owner||"").split(":")[1]||"";
  if(lane==="claude")return markImage("mark-claude.png","chat agent",chatMark());
  if(lane==="codex")return markImage("mark-codex.png","code agent",codeMark(`cm${markSeq++}`));
  if(lane==="local"){
    const fast=/gpu|mlx|metal|accel/.test(detail.toLowerCase());
    return markImage(fast?"mark-accelerator.png":"mark-compute.png",
                     fast?"local accelerator":"local compute",siliconMark(fast));
  }
  if(lane==="operator")return operatorMark();
  return "";
}
// The mark is the first thing every row says about itself and no surface said
// what it meant. The legend is rendered by calling ownerMark() with the same
// lane tokens the rows carry, so a mark cannot appear on a row and be absent
// from the legend, or drift from it: one function decides both.
const OWNER_LEGEND=[
  ["claude","chat agent"],
  ["codex","code agent"],
  ["local","local compute"],
  ["local:gpu","local accelerator"],
  ["operator","a person, not an agent"],
];
function ownerLegendHTML(){
  return `<p class="marklegend"><span class="marklegend-lead">Owner marks</span>`
    +OWNER_LEGEND.map(([owner,meaning])=>
      `<span class="marklegend-item">${ownerMark(owner)}<span>${esc(meaning)}</span></span>`).join("")
    +`<span class="marklegend-item marklegend-none">no mark<span>a lane this board does not name</span></span></p>`;
}

// One place holds what the board is currently showing. The detail plane, the
// filter and the keyboard model all read this rather than re-fetching or
// re-deriving, so they cannot disagree with the list about which rows exist.
const BOARD_REFRESH_MS=5000;
const BOARD_REQUEST_TIMEOUT_MS=4500;
const STATE={snapshot:null,graph:null,timeline:null,pulse:null,bundleProjection:null,edgesByNode:new Map(),view:"overview",section:"",selected:"",query:"",order:[],
  readSequence:0,readController:null,readTimer:0,readMeta:null,readFailure:""};

// ==========================================================================
// The Work surface -- grouped, capped, declared.
// --------------------------------------------------------------------------
// It used to render every row: 8,647 of them made a 333,000px document, and
// past ~220,000px Chromium stops compositing entirely -- the viewport paints
// the body background and nothing else, so jumping to a deep row showed an
// operator a black screen with no error anywhere. The cap is honest the same
// way the graph envelope is: each group renders up to CAP rows and then says
// exactly how many it is withholding, and one click expands that group.
//
// Density, grouping and sort are personal DISPLAY state. Layout is also stored
// locally, but rides the URL while Work is open so a shared or reloaded board
// preserves the presentation the sender was actually using.
// ==========================================================================
const WORK_CAP=120;
const DISPLAY=(()=>{
  const fallback={density:"comfortable",group:"status",layout:"board",sort:"source",workLayoutVersion:2};
  try{
    const saved=JSON.parse(localStorage.getItem("coord.display")||"{}");
    const value={...fallback,...saved};
    // Version 1 wrote "list" on every boot, even when the operator never chose
    // it. Migrate that accidental default once; subsequent choices are kept.
    if(saved.workLayoutVersion!==2)value.layout="board";
    if(!["list","board","timeline"].includes(value.layout))value.layout="board";
    if(value.layout==="board")value.group="status";
    return value;
  }
  catch(_e){return fallback;}
})();
const EXPANDED_GROUPS=new Set();

function storeDisplay(){
  try{localStorage.setItem("coord.display",JSON.stringify(DISPLAY));}catch(_e){/* full store */}
  document.documentElement.dataset.density=DISPLAY.density;
}

const STATUS_GROUPS=[
  ["running",   "Running",        r => String(r.status).toLowerCase() === "running"],
  ["blocked",   "Blocked",        r => String(r.status).toLowerCase() === "blocked"],
  ["next",      "Queued / Next",  r => ["queued","planned"].includes(String(r.status).toLowerCase())],
  ["done",      "Done / Recent",  r => ["done","archived","superseded","cancelled","canceled","closed","skipped"].includes(String(r.status).toLowerCase())],
  ["attention", "Needs attention",r => ["attention","failed"].includes(String(r.status).toLowerCase())],
];

function workEta(sec){
  if (sec == null) return "";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.round(sec / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function workRowHTML(row){
  const pct = row.progress_fraction == null ? null : Math.round(row.progress_fraction * 100);
  const status = String(row.status).toLowerCase();
  const tone = ["blocked","failed"].includes(status) ? "neg"
    : status === "running" ? "run"
    : status === "done" ? "ok" : "warn";
  const bare = String(row.id).replace(/^job:/, "");
  const title = row.title && row.title !== row.id && row.title !== bare ? row.title : bare;
  const sel = STATE.selected === row.id ? " sel" : "";
  return `<tr data-row="${esc(row.id)}" tabindex="0" role="button" aria-label="${esc(`Open ${title}, ${status}, ${row.id}`)}" class="wr${sel}">
    <td class="c-state"><i class="statedot ${tone}" title="${esc(status)}"></i></td>
    <td class="c-owner">${ownerMark(row.owner)}</td>
    <td class="c-work">${esc(title)}</td>
    <td class="c-module">${row.module ? `<span class="modpill">${esc(row.module)}</span>` : ""}</td>
    <td class="c-progress">${pct == null ? "" :
      `<span class="tbar ${tone}"><i data-w="${pct}"></i></span><span class="tpct">${pct}%</span>`}</td>
    <td class="c-eta">${esc(workEta(row.eta_seconds))}</td>
    <td class="c-note">${esc(row.current_step || "")}</td>
    <td class="c-prio">${row.priority ? `P${esc(String(row.priority))}` : ""}</td>
    <td class="c-res">${esc(row.group && row.group !== row.module ? row.group : "")}</td>
    <td class="c-id">${esc(row.id)}</td>
  </tr>`;
}

// The population, after the two narrowings in order of authority: the server
// decided what exists, the find box decides what of it is drawn.
function visibleWorkRows(){
  if(!STATE.snapshot)return [];
  return STATE.snapshot.rows.filter(row=>{
    const inPopulation=!SEM.matched||!SEM.joinable||SEM.matched.has(row.id);
    return inPopulation&&matchesQuery(row);
  });
}

function orderedVisibleWorkRows(){
  const rows=visibleWorkRows();
  if(DISPLAY.sort==="source")return rows;
  const sourceIndex=new Map((STATE.snapshot?.rows||[]).map((row,index)=>[row.id,index]));
  const tie=(left,right)=>(sourceIndex.get(left.id)??0)-(sourceIndex.get(right.id)??0);
  return [...rows].sort((left,right)=>{
    if(DISPLAY.sort==="priority")return (Number(right.priority)||0)-(Number(left.priority)||0)||tie(left,right);
    if(DISPLAY.sort==="title")return String(left.title||left.id).localeCompare(String(right.title||right.id))||tie(left,right);
    if(DISPLAY.sort==="status")return String(left.status||"").localeCompare(String(right.status||""))||tie(left,right);
    return tie(left,right);
  });
}

function workGroups(rows,includeEmpty=false){
  if(DISPLAY.group==="status"){
    const claimed=new Set();
    const groups=STATUS_GROUPS.map(([key,label,test])=>{
      const members=rows.filter(r=>!claimed.has(r.id)&&test(r));
      members.forEach(r=>claimed.add(r.id));
      return {key,label,members};
    });
    const rest=rows.filter(r=>!claimed.has(r.id));
    // Anything the named groups did not claim still belongs on the board; a
    // row that silently vanishes because its status is unrecognised is the
    // defect this trailing group exists to prevent.
    if(rest.length)groups.push({key:"other",label:"Other",members:rest});
    return includeEmpty?groups:groups.filter(group=>group.members.length);
  }
  const field=DISPLAY.group==="owner"?"owner":"module";
  const byKey=new Map();
  for(const row of rows){
    const key=String(row[field]||"").toLowerCase()||"(none)";
    if(!byKey.has(key))byKey.set(key,[]);
    byKey.get(key).push(row);
  }
  return [...byKey.entries()]
    .sort((a,b)=>b[1].length-a[1].length||(a[0]<b[0]?-1:1))
    .map(([key,members])=>({key:`${field}:${key}`,label:key,members}));
}

// An empty board and an over-narrow filter both render zero rows and are not
// the same answer. A stranger who runs the board before anything has been
// seeded was being told to "clear Find or adjust Filter" -- two controls that
// are already empty -- with no mention of the one command that would give the
// surface something to show. `summary.total` counts the whole board even when
// the graph envelope trimmed every row out of this particular read, so the
// three cases stay distinguishable.
function emptyWorkCopy(shared){
  const snapshot=STATE.snapshot;
  const carried=snapshot?snapshot.rows.length:0;
  const boardTotal=Number(snapshot&&snapshot.summary&&snapshot.summary.total!=null
    ? snapshot.summary.total:carried);
  if(!boardTotal){
    return `<b>This board has no rows yet.</b><p>Nothing has been claimed, handed off or recorded against it. Seed a demonstration board with <code>python -m coordharness.demo</code>, then reload.</p>`;
  }
  if(!carried){
    return `<b>This read carries none of the board's ${formatCount(boardTotal)} rows.</b><p>The graph envelope emitted no rows for this generation. Nothing is inferred from their absence.</p>`;
  }
  return `<b>No rows match this view.</b><p>${shared}</p>`;
}

function renderWorkList(){
  const mount=document.querySelector("#work");
  if(!mount||!STATE.snapshot)return;
  const rows=orderedVisibleWorkRows();
  const total=STATE.snapshot.rows.length;
  const narrowed=STATE.query.trim()||SEM.matched;
  const populationLabel=narrowed
    ?`${formatCount(rows.length)} of ${formatCount(total)} rows`:`${formatCount(total)} rows`;
  if(!rows.length){
    STATE.order=[];
    mount.innerHTML=`<div class="empty" role="status">${emptyWorkCopy("Clear Find or adjust Filter. A selected row may remain open from the shared URL even when the current population excludes it.")}</div>`;
    document.querySelector("#popcount").textContent=populationLabel;
    return;
  }
  const rendered=[];
  const sections=workGroups(rows).map(group=>{
    const open=EXPANDED_GROUPS.has(group.key);
    const shown=open?group.members:group.members.slice(0,WORK_CAP);
    // A selected row must stay rendered even past the cap, or following a
    // palette jump would land on a row that does not exist on screen.
    if(!open&&STATE.selected&&!shown.some(r=>r.id===STATE.selected)){
      const held=group.members.find(r=>r.id===STATE.selected);
      if(held)shown.push(held);
    }
    shown.forEach(r=>rendered.push(r.id));
    const withheld=group.members.length-shown.length;
    return `<tr class="grouphead ${esc(group.key.split(":")[0])}"><th colspan="10">${esc(group.label)} <span>${formatCount(group.members.length)}</span></th></tr>`
      + shown.map(workRowHTML).join("")
      + (withheld>0
        ? `<tr class="groupmore"><td colspan="10"><button type="button" data-expand-group="${esc(group.key)}">Show the ${formatCount(withheld)} more in ${esc(group.label)}</button></td></tr>`
        : "");
  }).join("");
  mount.innerHTML=`<div class="tablewrap"><table class="worktable">
    <thead><tr>
      <th class="c-state"></th><th class="c-owner"></th><th class="c-work">Work</th>
      <th class="c-module">Module</th><th class="c-progress">Progress</th><th class="c-eta">ETA</th>
      <th class="c-note">Note</th><th class="c-prio">Prio</th><th class="c-res">Resource</th>
      <th class="c-id">ID</th>
    </tr></thead>
    <tbody>${sections}</tbody>
  </table></div>`;
  mount.querySelectorAll("[data-w]").forEach(el=>{
    const w=Number(el.dataset.w);
    if(Number.isFinite(w))el.style.width=`${Math.max(0,Math.min(100,w))}%`;
  });
  STATE.order=rendered;
  document.querySelector("#popcount").textContent=populationLabel;
}

function workCardHTML(row){
  const status=String(row.status).toLowerCase();
  const terminal=["done","archived","superseded","cancelled","canceled","closed","skipped"].includes(status);
  const tone=["blocked","failed"].includes(status)?"neg":status==="running"?"run":terminal?"ok":"warn";
  const bare=String(row.id).replace(/^job:/,"");
  const title=row.title&&row.title!==row.id&&row.title!==bare?row.title:bare;
  const pct=row.progress_fraction==null?null:Math.round(row.progress_fraction*100);
  const owner=String(row.owner||"unassigned");
  const hasPriority=row.priority!==null&&row.priority!==undefined&&String(row.priority)!=="";
  const stale=Boolean(row.stale||STATE.snapshot?.stale);
  const signalText=[status,hasPriority?`priority P${row.priority}`:"",pct==null?"":`progress ${pct}%`,stale?"stale flag":"no stale flag"].filter(Boolean).join(", ");
  return `<button class="workcard${STATE.selected===row.id?" sel":""}" type="button" data-row="${esc(row.id)}" data-status="${esc(status)}" aria-label="${esc(`Open ${title}, ${row.id}, owner ${owner}, ${signalText}`)}">
    <span class="workcard-title">${esc(title)}</span>
    <span class="workcard-byline"><code>${esc(row.id)}</code><span class="workcard-owner">${ownerMark(owner)}<span>${esc(owner)}</span></span></span>
    <span class="workcard-signals" aria-hidden="true">
      <i class="statedot ${tone}" title="${esc(status)}"></i>
      ${hasPriority?`<span class="workcard-priority" title="Priority P${esc(String(row.priority))}">P${esc(String(row.priority))}</span>`:""}
      ${pct==null?"":`<progress max="100" value="${Math.max(0,Math.min(100,pct))}" title="Progress ${pct}%"></progress>`}
      <i class="workcard-freshness ${stale?"stale":"current"}" title="${stale?"Stale flag":"No stale flag"}"></i>
    </span>
  </button>`;
}

function workTruthDisclosure(){
  const snapshot=STATE.snapshot||{};
  const generated=snapshot.generated_at&&Number.isFinite(Date.parse(snapshot.generated_at))
    ?new Date(snapshot.generated_at).toLocaleString():"unknown time";
  return `<details class="work-contract"${STATE.workContractOpen?" open":""}>
    <summary><span>Board truth</span><span class="work-contract-receipt">Read-only · ${esc(snapshot.source||"published snapshot")}</span></summary>
    <div class="work-contract-copy"><p>Lane membership and counts use published status values from the filtered population, generated ${esc(generated)}. Cards only select the canonical detail; there is no drag-to-lifecycle mutation. This public projection excludes proof artifacts, review verdicts, claim fences, event bodies and context links.</p></div>
  </details>`;
}

function renderWorkBoard(){
  const mount=document.querySelector("#work");
  if(!mount||!STATE.snapshot)return;
  const rows=orderedVisibleWorkRows();
  const total=STATE.snapshot.rows.length;
  const narrowed=STATE.query.trim()||SEM.matched;
  document.querySelector("#popcount").textContent=narrowed
    ?`${formatCount(rows.length)} of ${formatCount(total)} rows`:`${formatCount(total)} rows`;
  if(!rows.length){
    STATE.order=[];
    mount.innerHTML=`<div class="work-board-shell">${workTruthDisclosure()}<div class="empty" role="status">${emptyWorkCopy("List and Board use the same filtered population.")}</div></div>`;
    return;
  }
  const groups=workGroups(rows,true);
  const rendered=[];
  mount.innerHTML=`<div class="work-board-shell">${workTruthDisclosure()}<div class="kanban" role="region" aria-label="Work status board" tabindex="0" data-population-count="${rows.length}">${groups.map(group=>{
    const open=EXPANDED_GROUPS.has(`board:${group.key}`);
    let shown=open?group.members:group.members.slice(0,WORK_CAP);
    if(!open&&STATE.selected&&!shown.some(row=>row.id===STATE.selected)){
      const held=group.members.find(row=>row.id===STATE.selected);if(held)shown=[...shown,held];
    }
    shown.forEach(row=>rendered.push(row.id));
    const withheld=group.members.length-shown.length;
    return `<section class="kanban-lane" data-lane="${esc(group.key)}" data-row-count="${group.members.length}" aria-labelledby="lane-${esc(group.key)}">
      <h3 id="lane-${esc(group.key)}"><span>${esc(group.label)}</span><b>${formatCount(group.members.length)}</b></h3>
      <div class="kanban-cards">${shown.map(workCardHTML).join("")}</div>
      ${withheld>0?`<button class="kanban-more" type="button" data-expand-group="board:${esc(group.key)}">Show ${formatCount(withheld)} more</button>`:""}
    </section>`;}).join("")}</div></div>`;
  STATE.order=rendered;
}

const TIMELINE_CAP=240;
function renderWorkTimeline(){
  const mount=document.querySelector("#work");
  if(!mount||!STATE.snapshot)return;
  const population=orderedVisibleWorkRows();
  const allowed=new Set(population.map(row=>row.id));
  const timeline=STATE.timeline;
  if(!timeline||!Array.isArray(timeline.items)){
    STATE.order=[];
    mount.innerHTML=`<div class="empty timeline-empty" role="status"><b>Timeline unavailable in compatibility mode.</b><p>This server did not publish coherent timeline metadata. List and Board remain available; no event times are inferred.</p></div>`;
    document.querySelector("#popcount").textContent=`0 represented of ${formatCount(population.length)} rows`;
    return;
  }
  const occurrences=[];let invalid=0;
  for(const item of timeline.items){
    if(!allowed.has(item.id))continue;
    for(const event of (Array.isArray(item.events)?item.events:[])){
      const at=Date.parse(event.at);
      if(!Number.isFinite(at)){invalid++;continue;}
      occurrences.push({id:item.id,at,atSource:event.at,actor:event.actor||"",kind:event.kind||""});
    }
  }
  occurrences.sort((left,right)=>right.at-left.at||stableCompare(left.id,right.id)||stableCompare(left.kind,right.kind));
  const total=occurrences.length;const shown=occurrences.slice(0,TIMELINE_CAP);
  const represented=new Set(shown.map(item=>item.id));STATE.order=[...represented];
  const groups=new Map();
  for(const item of shown){const day=new Date(item.at).toISOString().slice(0,10);if(!groups.has(day))groups.set(day,[]);groups.get(day).push(item);}
  const projectionOmitted=Number(STATE.bundleProjection?.timeline?.omitted||0);
  const omittedByCap=Math.max(0,total-shown.length);
  const receipt=`${formatCount(shown.length)} of ${formatCount(total)} occurrences · ${formatCount(represented.size)} of ${formatCount(population.length)} filtered rows represented`
    +(omittedByCap?` · ${formatCount(omittedByCap)} capped`:` · cap ${TIMELINE_CAP} not reached`)
    +(projectionOmitted?` · bundle projection omitted ${formatCount(projectionOmitted)} timeline row${projectionOmitted===1?"":"s"}`:"")
    +(invalid?` · ${formatCount(invalid)} invalid timestamps omitted`:"");
  mount.innerHTML=`<div class="timeline-receipt" data-timeline-total="${total}" data-timeline-shown="${shown.length}">${receipt}. Metadata source: ${esc(timeline.source||"bundle.timeline")}; event bodies are not published.</div>`
    +([...groups.entries()].map(([day,items])=>`<section class="timeline-day" data-day="${day}"><h3>${esc(new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined,{timeZone:"UTC",year:"numeric",month:"short",day:"numeric"}))} <span>${items.length}</span></h3><div>${items.map(item=>`<button type="button" class="timeline-event${STATE.selected===item.id?" sel":""}" data-row="${esc(item.id)}" data-at="${esc(item.atSource)}" role="button"><time datetime="${esc(item.atSource)}">${esc(new Date(item.at).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}))}</time><b>${esc(item.kind||"event")}</b><span>${esc(item.actor||"unknown actor")}</span><code>${esc(item.id)}</code></button>`).join("")}</div></section>`).join("")||`<div class="empty"><b>No timestamped events represent this filtered population.</b><p>No event time is inferred.</p></div>`);
  document.querySelector("#popcount").textContent=`${formatCount(represented.size)} of ${formatCount(population.length)} rows represented`;
}

// List, Board and Timeline are presentations of one semantic population and
// one canonical selection/detail plane. Layout and sort are personal display
// state, never a second route or an authority-changing workflow.
function renderWorkTable(){
  // Background snapshot refreshes replace the work DOM. Preserve the
  // operator disclosure state so a refresh cannot make a control appear
  // dead immediately after it was opened.
  const currentContract=document.querySelector("#work .work-contract");
  if(currentContract)STATE.workContractOpen=currentContract.open;
  syncWorkModes();
  if(DISPLAY.layout==="board")renderWorkBoard();
  else if(DISPLAY.layout==="timeline")renderWorkTimeline();
  else renderWorkList();
}

function render(snapshot,graph,timeline=null,bundleProjection=null){
  STATE.snapshot=snapshot;STATE.graph=graph;STATE.timeline=timeline;STATE.bundleProjection=bundleProjection;
  STATE.edgesByNode=indexEdges(graph);
  const summary=snapshot.summary;
  const metric=(value,label)=>`<div class="metric"><b>${value}</b><small>${label}</small></div>`;
  const dur=sec=>{if(sec==null)return"";if(sec<60)return`${Math.round(sec)}s`;const m=Math.round(sec/60);return m<60?`${m}m`:`${Math.floor(m/60)}h ${m%60}m`;};
  // The overview is the landing surface: it should answer "what is happening"
  // before it explains what contract it speaks. Progress and ETA are printed
  // only where a job actually reported them; a row that reports neither prints
  // neither, rather than a zero that would read as a measurement.
  const liveRow=row=>{
    const pct=row.progress_fraction==null?null:Math.round(row.progress_fraction*100);
    const eta=dur(row.eta_seconds);
    // A bar under a row that stopped is not progress toward anything. It keeps
    // the number, loses the accent, and says the fraction is where it halted.
    const status=String(row.status).toLowerCase();
    const stopped=["blocked","failed"].includes(status);
    const tone=stopped?"neg":status==="running"?"run":"warn";
    // A job row's title is often its own id restated; printing both reads as
    // two facts when there is one.
    const bare=String(row.id).replace(/^job:/,"");
    const title=row.title&&row.title!==row.id&&row.title!==bare?` ${esc(row.title)}`:"";
    return `<li class="live" data-row="${esc(row.id)}" tabindex="0" role="button" aria-label="${esc(`Open ${row.title||bare}, ${status}, ${row.id}`)}"><div class="lt"><span class="owner">${
      ownerMark(row.owner)}</span><b>${esc(row.id)}</b>${title}${
      row.stale?' <span class="staleflag">no recent report</span>':""}</div>${
      row.current_step?`<p class="ls">${esc(row.current_step)}</p>`:""}${
      pct==null?"":`<div class="lbar ${tone}"><i data-w="${pct}"></i></div><p class="lm">${
        stopped?`stopped at ${pct}%`:`${pct}%`}${
        eta&&!stopped?` - ${esc(eta)} left, as reported`:""}</p>`}</li>`;
  };
  const panelList=(rows,title,empty)=>`<section class="card livecard"><h3>${title}</h3>${
    rows.length?`<ul class="livelist">${rows.map(liveRow).join("")}</ul>`
               :`<p class="meta">${empty}</p>`}</section>`;
  const isRunning=row=>String(row.status).toLowerCase()==="running";
  const needsEye=row=>["attention","blocked","failed"].includes(String(row.status).toLowerCase());
  const running=snapshot.rows.filter(isRunning);
  const attention=snapshot.rows.filter(needsEye).sort((a,b)=>b.priority-a.priority).slice(0,8);
  const attentionTotal=snapshot.rows.filter(needsEye).length;
  document.querySelector("#overview").innerHTML=`<div class="metrics">${
    metric(summary.running,"running")}${metric(summary.attention,"status attention")}${
    metric(summary.next,"next")}${metric(summary.done,"done")}${metric(summary.total,"total")}</div>
    ${ownerLegendHTML()}
    <div class="twoup">${
      panelList(running,"Running now","Nothing holds a live claim on this board right now.")}${
      panelList(attention,attentionTotal>attention.length
        ? `Status-based attention <span class="of">${attention.length} of ${attentionTotal}</span>`
        : "Status-based attention",
        "No row is blocked, failed, or flagged for attention.")}</div>
    <p class="prov">NativeSnapshotV1 - ${esc(snapshot.source)} - generated ${
      esc(new Date(snapshot.generated_at).toLocaleString())}. This loopback board renders the same minimal, read-only contract consumed by the native apps.</p>`;
  // Widths land through the CSSOM, never through a style attribute: this
  // board sends style-src 'self' with no unsafe-inline, so an inline style is
  // dropped silently and the bar would render at zero with no error anywhere.
  document.querySelectorAll("#overview [data-w]").forEach(el => {
    const width = Number(el.dataset.w);
    if (Number.isFinite(width)) el.style.width = `${Math.max(0, Math.min(100, width))}%`;
  });
  const rowCard=row=>`<article class="card"><div>${badge(row.status)}</div><h3>${esc(row.title)}</h3><p class="meta">${esc(row.id)} - <span class="owner">${ownerMark(row.owner)}${esc(row.owner||"unassigned")}</span> - ${esc(row.bucket)}</p><p>${esc(row.current_step)}</p>${row.progress_fraction!=null?`<p class="meta">${Math.round(row.progress_fraction*100)}% complete</p>`:""}</article>`;
  renderWorkTable();
  document.querySelector("#jobs").replaceChildren(cards(
    snapshot.rows.filter(row=>String(row.bucket).toLowerCase().includes("job")),
    rowCard,
  ));
  document.querySelector("#graph").innerHTML=renderGraph(graph);
}
// Edges are looked up per selected node, so index once rather than scanning
// a graph that carries tens of thousands of edges on a real board.
function indexEdges(graph){
  const index=new Map();
  const push=(id,edge)=>{if(!id)return;if(!index.has(id))index.set(id,[]);index.get(id).push(edge);};
  for(const edge of (graph&&graph.edges)||[]){push(edge.source,edge);push(edge.target,edge);}
  return index;
}

let continuousCommsFrameBound=false;
const continuousCommsFrames=new WeakSet();
function resizeContinuousCommsFrame(frame,height){
  const value=Math.ceil(Math.max(280,Math.min(8000,Number(height)||0)));
  const previous=Number(frame.dataset.commsHeight||0);
  if(!value||Math.abs(value-previous)<2)return;
  frame.dataset.commsHeight=String(value);
  frame.style.height=`${value}px`;
}
function bindContinuousCommsFrames(){
  const frames=[...document.querySelectorAll("[data-comms-continuous]")];
  const requestFleetStatus=frame=>{
    if(frame.dataset.commsFrame!=="fleet")return;
    frame.contentWindow?.postMessage({type:"coord.continuous-comms.request-status"},location.origin);
  };
  for(const frame of frames){
    if(continuousCommsFrames.has(frame))continue;
    continuousCommsFrames.add(frame);
    frame.addEventListener("load",()=>{
      try{
        const root=frame.contentDocument?.documentElement;
        resizeContinuousCommsFrame(frame,root?.scrollHeight);
        if(root&&globalThis.ResizeObserver){
          const observer=new ResizeObserver(()=>resizeContinuousCommsFrame(frame,root.scrollHeight));
          observer.observe(root);
        }
      }catch(_error){}
      requestFleetStatus(frame);
    });
  }
  if(continuousCommsFrameBound){
    frames.forEach(requestFleetStatus);
    return;
  }
  continuousCommsFrameBound=true;
  addEventListener("message",event=>{
    if(event.origin!==location.origin)return;
    const target=[...document.querySelectorAll("[data-comms-continuous]")]
      .find(frame=>frame.contentWindow===event.source);
    if(!target)return;
    if(event.data?.type==="coord.continuous-comms.height"){
      resizeContinuousCommsFrame(target,event.data.height);
      return;
    }
    if(event.data?.type!=="coord.continuous-comms.fleet-status"||target.dataset.commsFrame!=="fleet")return;
    const count=Number(event.data.runningCount);
    if(!Number.isInteger(count)||count<0)return;
    const receipt=document.querySelector("[data-comms-fleet-status]");
    if(!receipt)return;
    receipt.textContent=count===0
      ?"No rows are recorded running in this projection."
      :`${formatCount(count)} ${count===1?"row is":"rows are"} recorded running in this projection.`;
    receipt.hidden=false;
  });
  frames.forEach(requestFleetStatus);
}

const COMMS_TRAFFIC={lane:"",actor:"",kind:"",selected:""};
const commsEventKey=event=>[event.at,event.kind,event.actor,event.to,event.row]
  .map(value=>String(value||"")).join("|");
const commsActorLane=actor=>String(actor||"").split(":")[0].trim().toLowerCase();
function commsOptions(values,current,label){
  return `<option value="">${esc(label)}</option>`+[...new Set(values.filter(Boolean))]
    .sort(stableCompare)
    .map(value=>`<option value="${esc(value)}"${value===current?" selected":""}>${esc(value)}</option>`)
    .join("");
}

// ---- public communications plane -----------------------------------------
// PulseV1 carries counts and routed lane names only. It intentionally omits
// event bodies, payloads, references and per-event receipt claims.
function renderComms(){
  const root=document.querySelector("#comms");
  if(!root)return;
  const pulse=STATE.pulse;
  if(!pulse){
    root.innerHTML='<div class="empty">Communication traffic is unavailable in this compatibility generation.</div>';
    return;
  }
  const counts=pulse.counts||{};
  const lanes=Array.isArray(pulse.lanes)?pulse.lanes.slice().sort((a,b)=>stableCompare(String(a.lane),String(b.lane))):[];
  const allTraffic=(Array.isArray(pulse.traffic)?pulse.traffic:[]).filter(item=>item&&item.from&&item.to&&Number(item.count)>0);
  const allRecent=(Array.isArray(pulse.recent)?pulse.recent:[]).filter(Boolean);
  const laneChoices=[...new Set([...lanes.map(item=>String(item.lane||"")),...allTraffic.flatMap(item=>[String(item.from||""),String(item.to||"")])].filter(Boolean))].sort(stableCompare);
  const actorChoices=[...new Set(allRecent.map(item=>String(item.actor||"")).filter(Boolean))].sort(stableCompare);
  const kindChoices=[...new Set([...(pulse.kinds||[]).map(item=>String(item.kind||"")),...allTraffic.map(item=>String(item.kind||"")),...allRecent.map(item=>String(item.kind||""))].filter(Boolean))].sort(stableCompare);
  if(COMMS_TRAFFIC.lane&&!laneChoices.includes(COMMS_TRAFFIC.lane))COMMS_TRAFFIC.lane="";
  if(COMMS_TRAFFIC.actor&&!actorChoices.includes(COMMS_TRAFFIC.actor))COMMS_TRAFFIC.actor="";
  if(COMMS_TRAFFIC.kind&&!kindChoices.includes(COMMS_TRAFFIC.kind))COMMS_TRAFFIC.kind="";
  const traffic=allTraffic.filter(item=>(!COMMS_TRAFFIC.lane||item.from===COMMS_TRAFFIC.lane||item.to===COMMS_TRAFFIC.lane)&&(!COMMS_TRAFFIC.kind||item.kind===COMMS_TRAFFIC.kind));
  const recent=allRecent.filter(item=>(!COMMS_TRAFFIC.lane||commsActorLane(item.actor)===COMMS_TRAFFIC.lane||item.to===COMMS_TRAFFIC.lane)&&(!COMMS_TRAFFIC.actor||item.actor===COMMS_TRAFFIC.actor)&&(!COMMS_TRAFFIC.kind||item.kind===COMMS_TRAFFIC.kind));
  const selected=recent.find(item=>commsEventKey(item)===COMMS_TRAFFIC.selected)||recent[0]||null;
  COMMS_TRAFFIC.selected=selected?commsEventKey(selected):"";
  const handoffs=allTraffic.filter(item=>item.kind==="handoff").reduce((sum,item)=>sum+Number(item.count||0),0);
  const audits=allTraffic.filter(item=>String(item.kind).startsWith("audit_")).reduce((sum,item)=>sum+Number(item.count||0),0);
  const routed=allTraffic.reduce((sum,item)=>sum+Number(item.count||0),0);
  const laneNames=[...new Set(traffic.flatMap(item=>[String(item.from||""),String(item.to||"")]).filter(Boolean))].sort(stableCompare);
  const width=720,height=440,cx=width/2,cy=height/2,rx=245,ry=150;
  const positions=new Map(laneNames.map((lane,index)=>{
    const angle=-Math.PI/2+(Math.PI*2*index/Math.max(1,laneNames.length));
    return [lane,{x:cx+Math.cos(angle)*rx,y:cy+Math.sin(angle)*ry}];
  }));
  const laneByName=new Map(lanes.map(item=>[String(item.lane||""),item]));
  const paths=traffic.map((item,index)=>{
    const source=positions.get(String(item.from));const target=positions.get(String(item.to));
    if(!source||!target)return null;
    const self=String(item.from)===String(item.to);
    const path=self
      ?`M ${source.x} ${source.y-34} C ${source.x+86} ${source.y-112}, ${source.x-86} ${source.y-112}, ${source.x} ${source.y-34}`
      :`M ${source.x} ${source.y} Q ${cx+(index%3-1)*28} ${cy+(index%2?24:-24)} ${target.x} ${target.y}`;
    const kind=String(item.kind||"handoff");
    const stroke=Math.max(1,Math.min(7,1+Math.log10(Number(item.count)+1)*1.45));
    return `<path class="comms-edge ${esc(kind)}" d="${path}" stroke-width="${stroke.toFixed(2)}" marker-end="url(#comms-arrow)"><title>${esc(`${item.from} to ${item.to}: ${formatCount(item.count)} recorded ${kind.replaceAll("_"," ")}`)}</title></path>`;
  }).filter(Boolean).join("");
  const nodes=laneNames.map(lane=>{
    const point=positions.get(lane);const row=laneByName.get(lane)||{};const live=Number(row.sessions_live||0)>0;
    return `<g class="comms-node${live?" live":""}" transform="translate(${point.x} ${point.y})"><circle r="34"/><text y="-2">${esc(lane)}</text><text class="count" y="15">${formatCount(row.events||0)} events</text></g>`;
  }).join("");
  const eventRows=recent.map(event=>{
    const kind=String(event.kind||"event");const row=String(event.row||"");const target=event.to?` → ${event.to}`:"";
    return `<button type="button" class="comms-event${event===selected?" selected":""}" data-comms-event="${esc(commsEventKey(event))}" aria-pressed="${event===selected}"><span class="comms-kind ${esc(kind)}"></span><span><b>${esc(`${event.actor||"unattributed"}${target}`)}</b><small>${esc(kind.replaceAll("_"," "))} · ${row?esc(row):"no published row"} · ${esc(event.at||"")}</small></span></button>`;
  }).join("");
  const eventDetail=selected?`<dl class="comms-event-detail">
    <div><dt>Actor</dt><dd>${esc(selected.actor||"unattributed")}</dd></div>
    <div><dt>Direction</dt><dd>${esc(`${commsActorLane(selected.actor)||"unattributed"}${selected.to?` → ${selected.to}`:" · no published destination"}`)}</dd></div>
    <div><dt>Kind</dt><dd>${esc(String(selected.kind||"event").replaceAll("_"," "))}</dd></div>
    <div><dt>Recorded</dt><dd>${esc(selected.at||"unknown")}</dd></div>
    <div><dt>Thread / work row</dt><dd>${selected.row?`<a href="#v=work&layout=list&sel=${encodeURIComponent(selected.row)}">${esc(selected.row)}</a>`:"not published"}</dd></div>
  </dl>`:`<p class="comms-empty">No recent recorded event matches these filters.</p>`;
  const trafficHtml=`<header class="comms-traffic-head"><div><span>Recorded traffic</span><h2>Direction and event feed</h2></div><p>Recorded direction only — not a live stream, live activity, or delivery status. No delivery, read, accepted, or response state is inferred.</p></header>
    <div class="comms-filters" aria-label="Traffic filters">
      <label>Lane<select data-comms-filter="lane">${commsOptions(laneChoices,COMMS_TRAFFIC.lane,"All lanes")}</select></label>
      <label>Actor<select data-comms-filter="actor">${commsOptions(actorChoices,COMMS_TRAFFIC.actor,"All actors")}</select></label>
      <label>Kind<select data-comms-filter="kind">${commsOptions(kindChoices,COMMS_TRAFFIC.kind,"All kinds")}</select></label>
      <button type="button" data-comms-reset>Reset</button>
    </div>
    <div class="comms-planes">
      <section class="comms-plane comms-direction" aria-label="Recorded-direction traffic visualization"><header><h2>Recorded direction</h2><span>${formatCount(traffic.reduce((sum,item)=>sum+Number(item.count||0),0))} routed acts</span></header>
        <div class="comms-map">${traffic.length?`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Recorded routed coordination acts"><defs><marker id="comms-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>${paths}${nodes}</svg>`:`<p class="comms-empty">No recorded directed route matches these filters.</p>`}</div>
      </section>
      <section class="comms-plane comms-feed" aria-label="Recorded event feed"><header><h2>Recent events</h2><span>${formatCount(recent.length)} of ${formatCount(allRecent.length)}</span></header><div class="comms-events">${eventRows||`<p class="comms-empty">No matching event in the published recent window.</p>`}</div></section>
      <aside class="comms-plane comms-detail" aria-label="Selected event and thread detail"><header><h2>Selected event / thread</h2><span>Published metadata only</span></header>${eventDetail}<p class="comms-detail-truth">Event bodies and per-event receipt claims are withheld from this public projection.</p></aside>
    </div>`;
  // Polling refreshes update the summary in place. Replacing this subtree
  // would destroy the three canonical Map browsing contexts every five seconds
  // and would duplicate event listeners in the native embed.
  if(root.querySelector(".comms-shell")){
    const values=[counts.events||0,routed,handoffs,audits,counts.sessions_live||0]
      .map(formatCount);
    root.querySelectorAll(".comms-kpi b").forEach((node,index)=>{
      if(values[index]!==undefined)node.textContent=values[index];
    });
    const workspace=root.querySelector("[data-comms-traffic-body]");
    if(workspace)workspace.innerHTML=trafficHtml;
    bindContinuousCommsFrames();
    return;
  }
  const continuousSurfaces={
    fleet:`<section class="comms-continuous" id="comms-fleet" aria-label="Full Fleet intelligence" data-comms-surface="fleet">
      <iframe class="comms-continuous-frame" data-comms-continuous
        data-comms-frame="fleet" src="/map?embedded=1&continuous=1&section=fleet" title="Full Fleet"
        loading="eager"></iframe>
    </section>`,
    deps:`<section class="comms-continuous" id="comms-dependencies" aria-label="Full Dependencies intelligence" data-comms-surface="deps">
      <iframe class="comms-continuous-frame" data-comms-continuous
        data-comms-frame="deps" src="/map?embedded=1&continuous=1&section=deps" title="Full Dependencies"
        loading="eager"></iframe>
    </section>`,
    pulse:`<section class="comms-continuous" id="comms-pulse" aria-label="Full Pulse intelligence" data-comms-surface="pulse">
      <iframe class="comms-continuous-frame" data-comms-continuous
        data-comms-frame="pulse" src="/map?embedded=1&continuous=1&section=pulse" title="Full Pulse"
        loading="eager"></iframe>
    </section>`,
  };
  root.innerHTML=`<div class="comms-shell">
    <nav class="comms-jump" aria-label="Comms page sections">
      <span>Comms</span>
      <button type="button" data-comms-jump="#comms-fleet">Fleet</button>
      <button type="button" data-comms-jump="#comms-traffic">Traffic</button>
      <button type="button" data-comms-jump="#comms-dependencies">Dependencies</button>
      <button type="button" data-comms-jump="#comms-pulse">Pulse</button>
    </nav>
    ${continuousSurfaces.fleet}
    <section class="comms-traffic-workspace" id="comms-traffic" data-comms-traffic aria-label="Recorded coordination traffic">
      <div class="comms-kpis">
        <div class="comms-kpi"><b>${formatCount(counts.events||0)}</b><span>recorded events</span></div>
        <div class="comms-kpi"><b>${formatCount(routed)}</b><span>routed acts</span></div>
        <div class="comms-kpi"><b>${formatCount(handoffs)}</b><span>handoffs</span></div>
        <div class="comms-kpi"><b>${formatCount(audits)}</b><span>audit exchanges</span></div>
        <div class="comms-kpi"><b>${formatCount(counts.sessions_live||0)}</b><span>live sessions</span></div>
      </div>
      <div data-comms-traffic-body>${trafficHtml}</div>
    </section>
    ${continuousSurfaces.deps}
    ${continuousSurfaces.pulse}
    <footer class="comms-fleet-status" data-comms-fleet-status hidden></footer>
  </div>`;
  root.addEventListener("change",event=>{
    const filter=event.target.closest?.("[data-comms-filter]");
    if(!filter)return;
    COMMS_TRAFFIC[filter.dataset.commsFilter]=filter.value;
    COMMS_TRAFFIC.selected="";
    renderComms();
  });
  root.addEventListener("click",event=>{
    const jump=event.target.closest?.("[data-comms-jump]");
    if(jump){
      root.querySelector(jump.dataset.commsJump)?.scrollIntoView({block:"start"});
      return;
    }
    if(event.target.closest?.("[data-comms-reset]")){
      Object.assign(COMMS_TRAFFIC,{lane:"",actor:"",kind:"",selected:""});
      renderComms();
      return;
    }
    const choice=event.target.closest?.("[data-comms-event]");
    if(!choice)return;
    COMMS_TRAFFIC.selected=choice.dataset.commsEvent||"";
    renderComms();
  });
  bindContinuousCommsFrames();
}

// ---- URL state capsule ---------------------------------------------------
// View, Work layout and selection live in the URL so a reload, a shared link
// and a switch to another surface all land back on the same presentation and
// row. Personal filter text rides with them; it narrows what is shown and never
// changes what exists.
function readCapsule(){
  const raw=new URLSearchParams((location.hash||"").replace(/^#/,""));
  const requestedView=raw.get("v")||"overview";
  const legacyCommsView=["fleet","pulse"].includes(requestedView);
  STATE.view=legacyCommsView?"comms":requestedView;
  if(legacyCommsView){
    STATE.section="";
    raw.set("v",STATE.view);
    const canonicalHash=`#${raw.toString()}`;
    if(location.hash!==canonicalHash)history.replaceState(null,"",canonicalHash);
  }
  if(STATE.view==="activity"){STATE.view="work";DISPLAY.layout="timeline";} // legacy hash alias
  const layout=raw.get("layout");
  if(["list","board","timeline"].includes(layout))DISPLAY.layout=layout;
  if(DISPLAY.layout==="board")DISPLAY.group="status";
  STATE.selected=raw.get("sel")||"";
  STATE.query=raw.get("q")||"";
  // The semantic token rides the capsule because it IS the population; the
  // free-text box does not, because it only narrows what is already drawn.
  STATE.pendingSemantic=raw.get("sq")||"";
}
function writeCapsule(){
  const raw=new URLSearchParams();
  raw.set("v",STATE.view);
  if(STATE.view==="work")raw.set("layout",DISPLAY.layout);
  if(STATE.selected)raw.set("sel",STATE.selected);
  if(STATE.query)raw.set("q",STATE.query);
  if(SEM.token)raw.set("sq",SEM.token);
  const next=`#${raw.toString()}`;
  if(location.hash!==next)history.replaceState(null,"",next);
}

// ---- destinations --------------------------------------------------------
// The list itself belongs to shell.js, which paints navigation on every page;
// this file paints panels on one. It used to keep a second list -- seven
// destinations grouped Work/More, feeding a rail that had been shipped hidden
// -- so the shared subnav and the command palette were free to disagree about
// what the product contains, and did. What is added here is what only this
// file can know: how many rows each destination currently holds.
const VIEW_COUNTS={
  attention:{
    count:s=>attentionRows(s).length,
    countLabel:s=>{
      const rows=attentionRows(s);const reasons=new Set(rows.map(entry=>entry.plane.id)).size;
      return `${formatCount(rows.length)} rows · ${formatCount(reasons)} reasons`;
    },urgent:true},
  work:{count:s=>s.rows.length},
  jobs:{count:s=>s.rows.filter(r=>String(r.bucket).toLowerCase().includes("job")).length},
  comms:{count:s=>Number(STATE.pulse?.counts?.events||0)},
};
const VIEWS=((typeof window!=="undefined"&&window.CoordNav&&window.CoordNav.boardPanels)||[])
  .map(panel=>({id:panel.id,label:panel.label,...(VIEW_COUNTS[panel.id]||{})}));

// Attention is a population, not a card. Rows are grouped by the plane that
// raised them, because a stalled job and a blocked claim are different claims
// on an operator's time and merging them loses which authority to answer to.
const ATTENTION_PLANES=[
  {id:"failed",  label:"Failed",             why:"Execution reported a failure. The receipt is the evidence.",
   test:r=>String(r.status).toLowerCase()==="failed"},
  {id:"blocked", label:"Blocked",            why:"Work cannot proceed until something else moves.",
   test:r=>String(r.status).toLowerCase()==="blocked"},
  {id:"stale",   label:"Running, no report", why:"A claim is held but nothing has reported recently. The lease may outlive the process.",
   test:r=>Boolean(r.stale)&&String(r.status).toLowerCase()==="running"},
  {id:"flagged", label:"Flagged for attention", why:"Marked by the lifecycle itself rather than derived from execution.",
   test:r=>String(r.status).toLowerCase()==="attention"},
];
function attentionRows(snapshot){
  const seen=new Set();const out=[];
  for(const plane of ATTENTION_PLANES){
    for(const row of snapshot.rows){
      if(seen.has(row.id)||!plane.test(row))continue;
      seen.add(row.id);out.push({row,plane});
    }
  }
  return out;
}

function matchesQuery(row){
  const q=STATE.query.trim().toLowerCase();
  if(!q)return true;
  return [row.id,row.title,row.owner,row.module,row.group,row.status,row.current_step]
    .some(value=>String(value||"").toLowerCase().includes(q));
}

function renderLocation(){
  const view=VIEWS.find(v=>v.id===STATE.view)||VIEWS.find(v=>v.id==="work")||{label:STATE.view};
  document.querySelector("#crumbs").innerHTML=
    `Work<span class="sep">/</span><b>${esc(view.label)}</b>`
    +(STATE.selected?`<span class="sep">/</span><span>${esc(STATE.selected)}</span>`:"");
}

function syncWorkModes(){
  const control=document.querySelector("#workmodes");if(!control)return;
  control.hidden=STATE.view!=="work";
  document.body.dataset.view=STATE.view;
  if(STATE.view==="work")document.body.dataset.workLayout=DISPLAY.layout;
  else delete document.body.dataset.workLayout;
  control.querySelectorAll("[data-work-layout]").forEach(button=>
    button.setAttribute("aria-pressed",String(button.dataset.workLayout===DISPLAY.layout)));
}
function setWorkLayout(layout){
  if(!["list","board","timeline"].includes(layout))return;
  DISPLAY.layout=layout;if(layout==="board")DISPLAY.group="status";
  EXPANDED_GROUPS.clear();storeDisplay();renderWorkTable();writeCapsule();
}
function setView(view){
  STATE.view=view;
  document.querySelectorAll(".panel").forEach(panel=>panel.classList.toggle("active",panel.id===view));
  syncWorkModes();renderLocation();writeCapsule();
}

// ---- exchanges: the coordination recorded against one row ----------------
// Lifecycle, telemetry, relationships and freshness describe a task. None of
// them describe the coordination, which is the only reason two agents need a
// board at all: a handoff from one lane to another, an audit requested, a
// verdict returned. Every one of those acts was already in this page's state
// and none of them were attached to the row they happened on.
//
// The per-row population comes from TimelineV1, which carries every recorded
// event for every row the bundle emits. PulseV1's `recent` is the obvious
// source and is the wrong one: it publishes the newest twelve events BOARD
// WIDE, which on the seeded board names ten rows out of the twenty-six that
// have events. A plane built on it renders empty for most rows, including the
// handoff and the audit exchange that motivate having the plane at all.
//
// What `recent` carries that the timeline does not is the destination.
// TimelineV1's event tuple is sealed at (at, kind, actor) by test, so the lane
// a handoff was addressed TO reaches the client only inside that window. It is
// joined on the full event tuple where it exists and declared missing where it
// does not, because "we do not know where this went" and "this went nowhere"
// are different facts and a blank would publish the second one.
const EXCHANGE_ROUTED=new Set(["handoff","audit_request","audit_verdict"]);
const EXCHANGE_CAP=12;
const exchangeKey=(row,event)=>[row,event.at,event.kind,event.actor]
  .map(value=>String(value??"")).join("|");
function exchangeDestinations(){
  const index=new Map();
  const recent=STATE.pulse&&Array.isArray(STATE.pulse.recent)?STATE.pulse.recent:[];
  for(const event of recent){
    if(!event||!event.to)continue;
    index.set(exchangeKey(event.row,event),String(event.to));
  }
  return index;
}
// null means this generation published no per-row event metadata at all, which
// is a different answer from an empty list and is rendered as one.
function rowExchanges(rowId){
  const timeline=STATE.timeline;
  if(!timeline||!Array.isArray(timeline.items))return null;
  const item=timeline.items.find(entry=>entry&&String(entry.id)===String(rowId));
  const events=item&&Array.isArray(item.events)?item.events:[];
  const destinations=exchangeDestinations();
  return events
    .map(event=>({
      at:String(event.at||""),
      kind:String(event.kind||"event"),
      actor:String(event.actor||""),
      to:destinations.get(exchangeKey(rowId,event))||"",
    }))
    .sort((left,right)=>stableCompare(right.at,left.at));
}
function exchangeWhen(at){
  const parsed=Date.parse(at);
  return Number.isFinite(parsed)?new Date(parsed).toLocaleString():at||"time not published";
}
function exchangePlane(rowId){
  const acts=rowExchanges(rowId);
  if(acts===null){
    return `<div class="dplane"><h3>Exchanges</h3><p class="dnote">This generation published no per-row event metadata, so no coordination act can be attached to this row. None is inferred.</p></div>`;
  }
  if(!acts.length){
    return `<div class="dplane"><h3>Exchanges</h3><p class="dnote">No coordination act is recorded against this row: nothing was handed off, requested or returned here. That is the record, not a gap in it.</p></div>`;
  }
  const shown=acts.slice(0,EXCHANGE_CAP);
  const routed=acts.filter(act=>EXCHANGE_ROUTED.has(act.kind)).length;
  const addressed=acts.filter(act=>act.to).length;
  const items=shown.map(act=>{
    const isRouted=EXCHANGE_ROUTED.has(act.kind);
    const actor=`<b>${esc(act.actor||"unattributed")}</b>`;
    const destination=act.to
      ? ` <span class="dexch-arrow" aria-hidden="true">&rarr;</span> <b>${esc(act.to)}</b>`
      : isRouted
        ? ` <span class="dexch-arrow" aria-hidden="true">&rarr;</span> <span class="missing">destination not in this read</span>`
        : "";
    return `<div class="dexch${isRouted?" routed":""}">`
      +`<span class="dexch-kind ${esc(act.kind)}" aria-hidden="true"></span>`
      +`<span class="dexch-who">${actor}${destination}</span>`
      +`<span class="dexch-what">${esc(act.kind.replaceAll("_"," "))}</span>`
      +`<time class="dexch-at" datetime="${esc(act.at)}">${esc(exchangeWhen(act.at))}</time>`
      +`</div>`;
  }).join("");
  const receipt=`${formatCount(acts.length)} recorded act${acts.length===1?"":"s"}`
    +(routed?` · ${formatCount(routed)} routed between lanes`:" · none routed between lanes")
    +` · ${formatCount(addressed)} name a destination`
    +(acts.length>shown.length?` · ${formatCount(acts.length-shown.length)} older not listed`:"");
  return `<div class="dplane"><h3>Exchanges</h3><div class="dexch-list">${items}</div>`
    +`<p class="dnote">${esc(receipt)}. A destination reaches this page only for the newest events board-wide; event bodies, refs and receipts are withheld from this public projection.</p></div>`;
}

// ---- canonical detail plane ---------------------------------------------
// One component, one row, planes kept visibly apart. It says what this read
// carries AND what it does not: a public read-only snapshot deliberately
// excludes proof, review verdicts and event bodies, and a detail pane that
// stayed silent about that would imply the row has none.
function renderDetail(){
  const pane=document.querySelector("#detail");
  const split=document.querySelector(".split");
  const row=STATE.selected&&STATE.snapshot
    ? STATE.snapshot.rows.find(candidate=>candidate.id===STATE.selected):null;
  if(!row){pane.hidden=true;split.classList.remove("showdetail");return;}
  pane.hidden=false;split.classList.add("showdetail");
  const snapshot=STATE.snapshot;
  const pct=row.progress_fraction==null?null:Math.round(row.progress_fraction*100);
  const field=(label,value)=>value===""||value==null?"":`<dt>${esc(label)}</dt><dd>${value}</dd>`;
  const plain=(label,value)=>field(label,value?esc(String(value)):"");
  const edges=(STATE.edgesByNode.get(row.id)||STATE.edgesByNode.get(`work:${row.id}`)||[]);
  const relations=edges.slice(0,24).map(edge=>{
    const outward=edge.source===row.id||edge.source===`work:${row.id}`;
    const other=outward?edge.target:edge.source;
    return `<div class="drel"><span class="rkind">${esc(edge.kind||"related")}</span>`
      +`${outward?"&rarr;":"&larr;"} ${esc(other||"(unnamed)")}</div>`;
  }).join("");
  pane.innerHTML=`
    <button class="dclose" type="button" id="dclose" aria-label="Close detail">Esc</button>
    <p class="did">${esc(row.id)}</p>
    <h2>${esc(row.title||row.id)}</h2>

    <div class="dplane"><h3>Lifecycle</h3><dl class="dfields">
      ${plain("Status",row.status)}${plain("Bucket",row.bucket)}
      ${plain("Owner",row.owner||"unassigned")}${plain("Module",row.module)}
      ${plain("Group",row.group)}${row.priority?plain("Priority",`P${row.priority}`):""}
    </dl></div>

    <div class="dplane"><h3>Execution telemetry</h3><dl class="dfields">
      ${pct==null?"":plain("Progress",`${pct}%`)}
      ${row.eta_seconds==null?"":plain("ETA",`${Math.round(row.eta_seconds)}s as reported`)}
      ${plain("Last step",row.current_step)}
      ${row.stale?field("Reporting",'<span class="missing">no recent report</span>'):""}
    </dl>${pct==null&&!row.current_step?'<p class="dnote">This row has reported no execution telemetry.</p>':""}</div>

    <div class="dplane"><h3>Relationships</h3>
      ${relations||'<p class="dnote">No edge in the graph names this row.</p>'}
      ${edges.length>24?`<p class="dnote">${edges.length-24} more not listed.</p>`:""}
    </div>

    ${exchangePlane(row.id)}

    <div class="dplane"><h3>Freshness</h3><dl class="dfields">
      ${plain("Source",snapshot.source)}
      ${plain("Generated",new Date(snapshot.generated_at).toLocaleString())}
      ${snapshot.stale?field("State",'<span class="missing">source declared stale</span>'):plain("State","not declared stale")}
      ${plain("Projection","public read-only")}
    </dl></div>

    <div class="dplane"><h3>Actions</h3><div id="dactions"><p class="dnote">Loading typed action preview&hellip;</p></div></div>`;
  document.querySelector("#dclose").addEventListener("click",()=>selectRow(""));
  // Only the two non-mutating actions are ever wired. The other seven are
  // rendered so their refusal reason is visible, and there is deliberately no
  // code path here that could run one.
  pane.addEventListener("click",event=>{
    const button=event.target.closest("[data-act]");
    if(!button)return;
    if(button.dataset.act==="copy_id"&&navigator.clipboard){
      navigator.clipboard.writeText(row.id).then(
        ()=>{button.textContent="Copied";setTimeout(()=>{button.textContent="Run";},1400);},
        ()=>{button.textContent="Blocked";});
      return;
    }
    if(button.dataset.act==="inspect"){
      const element=document.querySelector(`[data-row="${CSS.escape(row.id)}"]`);
      if(element&&element.scrollIntoView)element.scrollIntoView({block:"center"});
      button.textContent="Located";setTimeout(()=>{button.textContent="Run";},1400);
    }
  });
  renderActions(row.id);
}

// ==========================================================================
// Semantic query — Codex's sq1 contract, consumed rather than reimplemented
// --------------------------------------------------------------------------
// The population is decided by the server. This file composes a predicate,
// mints the canonical token for it, and renders whatever the endpoint returns.
// It never evaluates the predicate itself: a client-side filter that agreed
// with the server most of the time would be worse than no filter at all.
//
// The token's third segment is a plain SHA-256 of the payload, so a browser can
// mint one. It is an integrity checksum, not an authority: the server
// re-canonicalises and rejects anything that is not byte-identical to its own
// encoding, which is why the rules below are reproduced exactly.
// ==========================================================================
// `seq` fences responses. Clicking two chips quickly put two reads in flight,
// and the slower one landed last: the board showed 153 rows for a predicate
// the server had answered with 15. A wrong number that looks right is the
// worst thing this surface can produce.
const SEM={facets:{status:new Set(),module:new Set(),actor:new Set()},expanded:new Set(),seq:0,token:"",
  matched:null,receipt:null,joinable:true,error:"",views:[]};

// json.dumps(..., sort_keys=True, separators=(",",":"), ensure_ascii=True)
function canonicalJSON(value){
  if(value===null)return "null";
  if(Array.isArray(value))return `[${value.map(canonicalJSON).join(",")}]`;
  if(typeof value==="object")
    return `{${Object.keys(value).sort().map(key=>
      `${canonicalJSON(key)}:${canonicalJSON(value[key])}`).join(",")}}`;
  if(typeof value!=="string")return JSON.stringify(value);
  // ensure_ascii: every non-ASCII code unit escapes to \uXXXX.
  let out='"';
  for(const ch of value){
    const code=ch.codePointAt(0);
    if(ch==='"')out+='\\"';
    else if(ch==="\\")out+="\\\\";
    else if(code<0x20)out+=`\\u${code.toString(16).padStart(4,"0")}`;
    else if(code<0x7f)out+=ch;
    else for(let i=0;i<ch.length;i+=1)
      out+=`\\u${ch.charCodeAt(i).toString(16).padStart(4,"0")}`;
  }
  return `${out}"`;
}
function b64url(bytes){
  let binary="";
  for(const byte of bytes)binary+=String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
}
async function mintQueryToken(query){
  const payload=new TextEncoder().encode(canonicalJSON(query));
  const digest=new Uint8Array(await crypto.subtle.digest("SHA-256",payload));
  return `sq1.${b64url(payload)}.${b64url(digest)}`;
}
// Set members are lowercased, de-duplicated and sorted; `all` children are
// sorted by their own canonical bytes and de-duplicated, because the operator
// is commutative and idempotent and one predicate must have one token.
function buildQuery(){
  const children=[];
  for(const [operator,values] of Object.entries(SEM.facets)){
    if(!values.size)continue;
    const members=[...new Set([...values].map(value=>String(value).toLowerCase()))].sort();
    children.push({[operator]:{in:members}});
  }
  const unique=new Map(children.map(child=>[canonicalJSON(child),child]));
  const ordered=[...unique.keys()].sort().map(key=>unique.get(key));
  return {expr:{all:ordered},schema_version:"SemanticQueryV1"};
}
function activeFacetCount(){
  return Object.values(SEM.facets).reduce((total,set)=>total+set.size,0);
}

async function runSemanticQuery(){
  const ticket=(SEM.seq+=1);
  if(!activeFacetCount()){
    SEM.token="";SEM.matched=null;SEM.receipt=null;SEM.error="";SEM.joinable=true;
    writeCapsule();renderFilterPanel();applyFilter();return;
  }
  try{
    const token=await mintQueryToken(buildQuery());
    const response=await fetch(`/api/v1/query?q=${encodeURIComponent(token)}`,
      {headers:{Accept:"application/json"}});
    const document_=await response.json();
    if(ticket!==SEM.seq)return;   // a newer predicate is already in flight
    SEM.token=token;
    if(!response.ok){
      SEM.error=`${document_.error||"query refused"}: ${document_.message||response.status}`;
      SEM.matched=null;
    }else{
      // Join only on the same read. The endpoint names the snapshot it
      // evaluated against; blending its ids into a different snapshot's rows
      // would produce a plausible list that never existed.
      const evaluated=(document_.source||{}).snapshot_generated_at;
      SEM.joinable=!evaluated||!STATE.snapshot||evaluated===STATE.snapshot.generated_at;
      SEM.matched=new Set(document_.matched_ids||[]);
      SEM.receipt=document_.omission_receipt||null;
      SEM.population=document_.population||null;
      SEM.error=SEM.joinable?"":"Query answered against a different snapshot than the one on screen.";
    }
  }catch(error){
    if(ticket!==SEM.seq)return;
    SEM.error=`query failed: ${error.message}`;SEM.matched=null;
  }
  if(ticket!==SEM.seq)return;
  writeCapsule();renderFilterPanel();applyFilter();
}

// Restoring from a link means asking the endpoint the same question again,
// then reading the facets back out of the query it echoes. The token is the
// contract; the chips are only a way of writing one.
async function restoreSemanticToken(token){
  try{
    const response=await fetch(`/api/v1/query?q=${encodeURIComponent(token)}`,
      {headers:{Accept:"application/json"}});
    const document_=await response.json();
    if(!response.ok){SEM.error=`${document_.error||"query refused"}: ${document_.message||""}`;renderFilterPanel();return;}
    Object.keys(SEM.facets).forEach(field=>SEM.facets[field].clear());
    for(const child of ((document_.query||{}).expr||{}).all||[]){
      const [operator]=Object.keys(child);
      if(SEM.facets[operator])
        (child[operator].in||[]).forEach(value=>SEM.facets[operator].add(value));
    }
    SEM.token=document_.query_token||token;
    SEM.matched=new Set(document_.matched_ids||[]);
    SEM.receipt=document_.omission_receipt||null;
    SEM.population=document_.population||null;
    const evaluated=(document_.source||{}).snapshot_generated_at;
    SEM.joinable=!evaluated||!STATE.snapshot||evaluated===STATE.snapshot.generated_at;
    SEM.error=SEM.joinable?"":"Query answered against a different snapshot than the one on screen.";
    renderFilterPanel();applyFilter();
  }catch(error){SEM.error=`query failed: ${error.message}`;renderFilterPanel();}
}

// Ordered by how many rows carry the value, not alphabetically. Alphabetical
// order with a cap puts whichever values sort first on screen and leaves the
// board's biggest modules unreachable, with nothing saying they had been
// dropped -- a silent truncation that reads as a complete list.
function facetValues(field){
  const rows=STATE.snapshot?STATE.snapshot.rows:[];
  const key=field==="actor"?"owner":field;
  const counts=new Map();
  for(const row of rows){
    const value=String(row[key]||"").toLowerCase();
    if(value)counts.set(value,(counts.get(value)||0)+1);
  }
  const ordered=[...counts.entries()]
    .sort((left,right)=>right[1]-left[1]||(left[0]<right[0]?-1:1));
  return {ordered,total:ordered.length};
}

function loadSavedViews(){
  try{SEM.views=JSON.parse(localStorage.getItem("coord.views")||"[]");}
  catch(_error){SEM.views=[];}
}
function storeSavedViews(){
  try{localStorage.setItem("coord.views",JSON.stringify(SEM.views.slice(0,24)));}
  catch(_error){/* a full or blocked store must not break the board */}
}

function renderFilterPanel(){
  const panel=document.querySelector("#filterpanel");
  const button=document.querySelector("#filterbtn");
  const active=activeFacetCount();
  button.classList.toggle("on",Boolean(active));
  button.innerHTML=`Filter${active?`<span class="fcount">${active}</span>`:""}`;
  if(panel.hidden)return;
  const group=(field,label)=>{
    const {ordered,total}=facetValues(field);
    if(!ordered.length)return "";
    const shown=SEM.expanded.has(field)?ordered:ordered.slice(0,14);
    // Any value already chosen stays on screen even when the cap would drop
    // it, or a shared link's filter would render as an empty chip row.
    const chosen=ordered.filter(([value])=>SEM.facets[field].has(value)
      &&!shown.some(([candidate])=>candidate===value));
    const rest=total-shown.length-chosen.length;
    return `<div class="fgroup"><span class="flabel">${esc(label)}</span>${
      [...shown,...chosen].map(([value,count])=>
        `<button class="fchip" type="button" data-facet="${field}" data-value="${esc(value)}" `
        +`aria-pressed="${SEM.facets[field].has(value)}">${esc(value)}`
        +`<span class="fn">${formatCount(count)}</span></button>`).join("")}${
      rest>0?`<button class="fchip fmore" type="button" data-expand="${field}">`
        +`+${formatCount(rest)} more</button>`:""}</div>`;
  };
  const receipt=SEM.receipt||{};
  const pop=SEM.population||{};
  const state=SEM.error
    ?`<span class="receipt warn">${esc(SEM.error)}</span>`
    :SEM.matched
      ?`<span class="receipt"><b>${formatCount(pop.matched??SEM.matched.size)}</b> matched of `
        +`<b>${formatCount(pop.rows??0)}</b> rows &nbsp;·&nbsp; ids complete: `
        +`${receipt.matched_ids_complete?"yes":"<span class=\"warn\">no</span>"}</span>`
      :`<span class="receipt">No semantic filter. The whole population is shown.</span>`;
  panel.innerHTML=`${group("status","Status")}${group("module","Module")}${group("actor","Owner")}
    <div class="frow">${state}
      <button class="fsave" type="button" id="fclear">Clear</button>
      <button class="fsave" type="button" id="fsave"${SEM.matched?"":" disabled"}>Save view</button>
      <span class="fviews">${SEM.views.map((view,index)=>
        `<button class="fview" type="button" data-view-index="${index}">${esc(view.name)}`
        +`<span class="x" data-drop="${index}">&times;</span></button>`).join("")}</span>
    </div>
    ${SEM.token?`<p class="ftoken">${esc(SEM.token)}</p>`:""}`;
}

// ==========================================================================
// Typed action registry — nine declared actions, two available, seven refused
// with the reason the server gives. The reasons are the point: a greyed-out
// button that will not say why is indistinguishable from a broken one.
// ==========================================================================
async function renderActions(id){
  const mount=document.querySelector("#dactions");
  if(!mount)return;
  try{
    const response=await fetch(`/api/v1/actions?target=${encodeURIComponent(id)}`,
      {headers:{Accept:"application/json"}});
    const document_=await response.json();
    if(!response.ok){mount.innerHTML=`<p class="dnote">${esc(document_.message||"actions unavailable")}</p>`;return;}
    STATE.actionsCache={id,doc:document_};
    if(STATE.selected!==id)return;   // selection moved while this was in flight
    const counts=document_.counts||{};
    mount.innerHTML=`<div class="dact">${(document_.actions||[]).map(action=>{
      const on=action.available;
      const failed=(action.checks||[]).filter(check=>!check.passed);
      const why=action.reason||failed.map(check=>check.reason).join(" ");
      return `<div class="dact-row ${on?"on":"off"}">
        <span class="amark">${on?"available":"refused"}</span>
        <span class="aname">${esc(action.label||action.id)}</span>
        ${on&&!action.mutation?`<button class="actbtn" type="button" data-act="${esc(action.id)}">Run</button>`:""}
        ${on?"":`<span class="areason">${esc(why)}</span>`}</div>`;
    }).join("")}</div>
    <p class="dnote">${formatCount(counts.declared||0)} declared &middot;
      ${formatCount(counts.available||0)} available &middot;
      ${formatCount(counts.reachable_mutations||0)} reachable mutations.
      This board is ${document_.source&&document_.source.read_only?"read-only":"writable"};
      authorization is <b>${esc(document_.authorization||"unknown")}</b>.</p>`;
  }catch(error){
    mount.innerHTML=`<p class="dnote">Action preview unavailable: ${esc(error.message)}</p>`;
  }
}

// The other product areas open on the same subject: their links carry the
// selection so Mesh, Map and Atlas land where the operator already is. Called
// from selectRow AND from the boot restore -- a capsule-restored selection
// never passes through selectRow, and the first version only rewired clicks.
function updateAreaLinks(){
  const id=STATE.selected;
  document.querySelectorAll(".shell-nav a").forEach(link=>{
    const base=link.getAttribute("href").split("#")[0];
    if(base==="/")return;
    link.setAttribute("href",id?`${base}#sel=${encodeURIComponent(id)}`:base);
  });
}

function selectRow(id){
  STATE.selected=id;
  updateAreaLinks();
  // The table renders a capped subset, so the selected row may not be in the
  // DOM yet; the renderer force-includes the selection, which is why it runs
  // before the class toggle rather than after.
  if(id&&!document.querySelector(`#work [data-row="${CSS.escape(id)}"]`))renderWorkTable();
  document.querySelectorAll("[data-row]").forEach(element=>
    element.classList.toggle("sel",Boolean(id)&&element.dataset.row===id));
  renderDetail();renderLocation();writeCapsule();
}

// ---- attention workspace -------------------------------------------------
function renderAttention(){
  const snapshot=STATE.snapshot;
  if(!snapshot)return;
  const population=attentionRows(snapshot);
  const all=population.filter(entry=>matchesQuery(entry.row));
  const total=population.length;
  const reasons=new Set(population.map(entry=>entry.plane.id)).size;
  const byPlane=new Map(ATTENTION_PLANES.map(plane=>[plane.id,[]]));
  all.forEach(entry=>byPlane.get(entry.plane.id).push(entry.row));
  const tone=status=>["blocked","failed"].includes(status)?"neg":status==="running"?"run":"warn";
  const body=ATTENTION_PLANES.map(plane=>{
    const rows=byPlane.get(plane.id);
    if(!rows.length)return "";
    return `<section class="attn-plane"><h3>${esc(plane.label)} <span class="n">${rows.length}</span></h3>
      <p class="why">${esc(plane.why)}</p>
      <ul class="attn-list">${rows.map(row=>
        `<li data-row="${esc(row.id)}" tabindex="0" role="button" aria-label="${esc(`Open ${row.title||row.id}, ${plane.label}, ${row.id}`)}" class="${STATE.selected===row.id?"sel":""}">`
        +`<i class="statedot ${tone(String(row.status).toLowerCase())}"></i>`
        +`<span class="at">${esc(row.title||row.id)}</span>`
        +`<span class="ai">${esc(row.id)}</span></li>`).join("")}</ul></section>`;
  }).join("");
  document.querySelector("#attention").innerHTML=body
    ||`<div class="empty">Nothing is failed, blocked, silently held, or flagged.</div>`;
  document.querySelector("#attention").insertAdjacentHTML("beforeend",
    `<p class="keyhint"><kbd>j</kbd><kbd>k</kbd> move &nbsp; <kbd>Enter</kbd> open &nbsp; <kbd>Esc</kbd> close &nbsp; <kbd>/</kbd> filter</p>`);
  document.querySelector("#popcount").textContent=STATE.query.trim()
    ?`${formatCount(all.length)} of ${formatCount(total)} decision rows · ${formatCount(reasons)} reasons`
    :`${formatCount(total)} decision rows · ${formatCount(reasons)} reasons`;
  STATE.order=all.map(entry=>entry.row.id);
}

// The filter narrows what is drawn. It never changes the population, and the
// count says both numbers so a narrowed list cannot be read as a smaller board.
// Re-rendered from data rather than toggled in the DOM: the table only holds
// the capped render, so DOM rows are not the population and counting them
// would understate every narrowing.
function applyFilter(){
  renderWorkTable();
  if(STATE.view==="attention")renderAttention();
  if(STATE.view==="attention"&&STATE.snapshot){
    STATE.order=attentionRows(STATE.snapshot).filter(entry=>matchesQuery(entry.row)).map(entry=>entry.row.id);
  }
}

// ---- keyboard model ------------------------------------------------------
function moveSelection(step){
  const order=STATE.order;
  if(!order.length)return;
  const at=order.indexOf(STATE.selected);
  const next=at===-1?(step>0?0:order.length-1):Math.min(order.length-1,Math.max(0,at+step));
  selectRow(order[next]);
  const element=document.querySelector(`[data-row="${CSS.escape(order[next])}"]`);
  if(element&&element.scrollIntoView)element.scrollIntoView({block:"nearest",inline:"nearest"});
}

// ==========================================================================
// Command palette -- one entry point for actions, destinations and rows.
// It searches; it never mutates. Actions resolve through the registry, and
// the seven refused mutations surface their reasons here exactly as they do
// in the detail plane -- a palette that hid them would imply a smaller
// vocabulary than the system has.
// ==========================================================================
const CMDK={open:false,items:[],sel:0,trigger:null};

function paletteItems(query){
  const q=query.trim().toLowerCase();
  const hit=text=>!q||String(text).toLowerCase().includes(q);
  const items=[];
  // Actions on the selected row come first: the palette opens over a context.
  const cache=STATE.actionsCache;
  if(STATE.selected&&cache&&cache.id===STATE.selected){
    for(const action of cache.doc.actions||[]){
      if(!hit(`${action.label} ${action.id}`))continue;
      const failed=(action.checks||[]).filter(check=>!check.passed);
      items.push({
        section:`Actions on ${STATE.selected}`,
        title:action.label||action.id,
        meta:action.available?"run":"refused",
        refused:!action.available,
        reason:action.available?"":(action.reason||failed.map(check=>check.reason).join(" ")),
        run:action.available?()=>runSafeAction(action.id):null,
      });
    }
  }
  for(const view of VIEWS){
    if(!hit(`go ${view.label}`))continue;
    items.push({section:"Views",title:`Go to ${view.label}`,meta:"view",
      run:()=>{setView(view.id);applyFilter();}});
  }
  for(const [label,href] of [["Mesh","/mesh"],["Map","/map"],["Operations Atlas","/ops"]]){
    if(!hit(`open ${label}`))continue;
    items.push({section:"Areas",title:`Open ${label}`,meta:"area",
      run:()=>{window.location.href=STATE.selected?`${href}#sel=${encodeURIComponent(STATE.selected)}`:href;}});
  }
  if(q&&STATE.snapshot){
    // Ranked, capped, and the cap is declared -- a truncated list that does
    // not say so reads as the whole answer.
    const scored=[];
    for(const row of STATE.snapshot.rows){
      const id=String(row.id).toLowerCase();
      const title=String(row.title||"").toLowerCase();
      const score=id.startsWith(q)?0:id.includes(q)?1:title.includes(q)?2:-1;
      if(score>=0)scored.push({row,score});
    }
    scored.sort((a,b)=>a.score-b.score||(a.row.id<b.row.id?-1:1));
    for(const {row} of scored.slice(0,12)){
      items.push({section:scored.length>12?`Rows (12 of ${formatCount(scored.length)})`:"Rows",
        title:row.title||row.id,meta:row.id,
        run:()=>{if(!["work","attention","jobs"].includes(STATE.view)){setView("work");}
          selectRow(row.id);applyFilter();
          const element=document.querySelector(`[data-row="${CSS.escape(row.id)}"]`);
          if(element&&element.scrollIntoView)element.scrollIntoView({block:"center"});}});
    }
  }
  return items;
}

function runSafeAction(id){
  const button=document.querySelector(`#dactions [data-act="${CSS.escape(id)}"]`);
  if(button){button.click();return;}
  if(id==="copy_id"&&navigator.clipboard&&STATE.selected)
    navigator.clipboard.writeText(STATE.selected).catch(()=>{});
}

function renderPalette(){
  const list=document.querySelector("#cmdk-list");
  const input=document.querySelector("#cmdk-input");
  if(!CMDK.items.length){
    list.innerHTML=`<p class="cmdk-empty">Nothing matches. Rows search needs at least one character.</p>`;
    input.removeAttribute("aria-activedescendant");
    return;
  }
  CMDK.sel=Math.max(0,Math.min(CMDK.items.length-1,CMDK.sel));
  let lastSection="";
  list.innerHTML=CMDK.items.map((item,index)=>{
    const head=item.section!==lastSection?`<p class="cmdk-sec">${esc(item.section)}</p>`:"";
    lastSection=item.section;
    return `${head}<div id="cmdk-option-${index}" class="cmdk-item ${index===CMDK.sel?"sel":""} ${item.refused?"refused":""}" data-idx="${index}" role="option" aria-selected="${index===CMDK.sel}" tabindex="-1">
      <span class="ct">${esc(item.title)}</span><span class="cm">${esc(item.meta)}</span>
      ${item.reason?`<span class="creason">${esc(item.reason)}</span>`:""}</div>`;
  }).join("");
  const selected=list.querySelector(".cmdk-item.sel");
  if(selected)input.setAttribute("aria-activedescendant",selected.id);
  else input.removeAttribute("aria-activedescendant");
  if(selected&&selected.scrollIntoView)selected.scrollIntoView({block:"nearest"});
}

function refreshPalette(){
  CMDK.items=paletteItems(document.querySelector("#cmdk-input").value);
  // Land on the first runnable item, not a refused one.
  CMDK.sel=Math.max(0,CMDK.items.findIndex(item=>item.run));
  renderPalette();
}

function openPalette(){
  if(CMDK.open)return;
  CMDK.trigger=document.activeElement instanceof HTMLElement?document.activeElement:null;
  CMDK.open=true;
  const overlay=document.querySelector("#cmdk");
  overlay.hidden=false;
  const input=document.querySelector("#cmdk-input");
  input.setAttribute("aria-expanded","true");
  input.value="";
  // Warm the actions section if a row is selected and nothing is cached yet.
  if(STATE.selected&&(!STATE.actionsCache||STATE.actionsCache.id!==STATE.selected))
    renderActions(STATE.selected).then(()=>{if(CMDK.open)refreshPalette();});
  refreshPalette();
  input.focus();
}
function closePalette(){
  if(!CMDK.open)return;
  CMDK.open=false;
  document.querySelector("#cmdk").hidden=true;
  const input=document.querySelector("#cmdk-input");
  input.setAttribute("aria-expanded","false");
  input.removeAttribute("aria-activedescendant");
  const trigger=CMDK.trigger;
  CMDK.trigger=null;
  if(trigger&&trigger.isConnected&&!trigger.closest("#cmdk"))trigger.focus();
}

function trapPaletteFocus(event){
  if(event.key!=="Tab")return false;
  const focusable=[...document.querySelectorAll("#cmdk input:not([disabled]),#cmdk button:not([disabled])")]
    .filter(element=>element.getClientRects().length);
  if(!focusable.length)return false;
  event.preventDefault();
  const at=focusable.indexOf(document.activeElement);
  const next=event.shiftKey?(at<=0?focusable.length-1:at-1):(at===focusable.length-1?0:at+1);
  focusable[next].focus();
  return true;
}

class BoardReadError extends Error{
  constructor(message,status=0){super(message);this.name="BoardReadError";this.status=status;}
}

async function fetchBoardJSON(url,signal){
  const response=await fetch(url,{headers:{Accept:"application/json"},cache:"no-store",signal});
  if(!response.ok)throw new BoardReadError(`${url} returned HTTP ${response.status}`,response.status);
  const value=await response.json();
  if(!value||typeof value!=="object"||Array.isArray(value))throw new BoardReadError(`${url} returned a non-document`);
  return value;
}

function validateBundle(bundle){
  if(!["OpsAtlasBundleV1","OpsAtlasBundleV2"].includes(bundle.schema_version))throw new BoardReadError("operations bundle schema is unsupported");
  for(const name of ["snapshot","graph","read_status"]){
    if(!bundle[name]||typeof bundle[name]!=="object"||Array.isArray(bundle[name]))
      throw new BoardReadError(`operations bundle is missing ${name}`);
  }
  const top=Number(bundle.cache_generation);
  const receipt=Number(bundle.read_status.cache_generation);
  if(!Number.isFinite(top)||!Number.isFinite(receipt)||top!==receipt)
    throw new BoardReadError("operations bundle and read-status generations disagree");
  return {
    snapshot:bundle.snapshot,
    graph:bundle.graph,
    timeline:bundle.timeline||null,
    pulse:bundle.pulse||null,
    bundleProjection:bundle.bundle_projection||null,
    meta:{mode:"bundle",cacheGeneration:top,readStatus:bundle.read_status,
      pulseCoherent:bundle.schema_version==="OpsAtlasBundleV2",
      generatedAt:bundle.snapshot.generated_at||bundle.read_status.source_generated_at||""},
  };
}

async function readBoardDocuments(signal){
  for(const endpoint of ["/api/v2/operations-bundle","/api/v1/operations-bundle"]){
    try{
      const document=validateBundle(await fetchBoardJSON(endpoint,signal));
      if(endpoint.includes("/v2/")&&!document.pulse)throw new BoardReadError("V2 bundle is missing Pulse");
      if(endpoint.includes("/v1/")){
        document.pulse=await fetchBoardJSON("/api/v1/pulse",signal).catch(error=>{
          if(error instanceof BoardReadError&&[404,405].includes(error.status))return null;throw error;
        });
        document.meta.pulseCoherent=false;
      }
      return document;
    }catch(error){
      // Compatibility is only for a server that genuinely lacks the endpoint.
      // A present-but-broken coherent bundle fails closed instead of silently
      // replacing one bad read with documents from unknown generations.
      if(!(error instanceof BoardReadError)||![404,405].includes(error.status))throw error;
    }
  }
  const timelineRead=fetchBoardJSON("/api/v1/timeline",signal).catch(error=>{
    if(error instanceof BoardReadError&&[404,405].includes(error.status))return null;throw error;
  });
  const [snapshot,graph,timeline]=await Promise.all([
    fetchBoardJSON("/api/v1/snapshot",signal),
    fetchBoardJSON("/api/v1/graph",signal),
    timelineRead,
  ]);
  return {snapshot,graph,timeline,bundleProjection:null,meta:{mode:"unbundled",cacheGeneration:null,readStatus:null,
    generatedAt:snapshot.generated_at||""}};
}

function boardReadTime(value){
  if(!value)return "unknown time";
  const date=new Date(value);
  return Number.isNaN(date.valueOf())?"unknown time":date.toLocaleTimeString();
}

function renderBoardReadState(){
  const health=document.querySelector("#health");
  const alert=document.querySelector("#boardreadalert");
  const dot=document.querySelector(".pulse span");
  const meta=STATE.readMeta;
  const serverDegraded=meta?.readStatus?.degraded===true;
  if(STATE.readFailure){
    if(STATE.snapshot){
      health.textContent="DEGRADED · last good";
      alert.textContent=`DEGRADED READ: refresh failed; retaining the last-good ${meta?.mode==="bundle"?"coherent generation":"compatibility read"} from ${boardReadTime(meta?.generatedAt)}. ${STATE.readFailure}. This board does not claim current live state.`;
    }else{
      health.textContent="Board unavailable";
      alert.textContent=`BOARD UNAVAILABLE: ${STATE.readFailure}. No last-good rows are available.`;
    }
    alert.hidden=false;
    dot.style.background="var(--red)";
    return;
  }
  if(serverDegraded){
    const failures=Number(meta.readStatus.consecutive_refresh_failures||0);
    health.textContent="DEGRADED · retained cache";
    alert.textContent=`DEGRADED READ: the server retained its last-good coherent generation after ${failures} refresh failure${failures===1?"":"s"}. Generated ${boardReadTime(meta.generatedAt)}; this board does not claim current live state.`;
    alert.hidden=false;
    dot.style.background="var(--amber)";
    return;
  }
  if(meta?.mode==="bundle"){
    health.textContent=`Live · gen ${formatCount(meta.cacheGeneration)}`;
    alert.hidden=true;
    alert.textContent="";
    dot.style.background="var(--green)";
    return;
  }
  if(meta?.mode==="unbundled"){
    health.textContent="UNBUNDLED · read-only";
    alert.textContent="COMPATIBILITY READ: this server has no coherent operations bundle, so snapshot and graph were fetched separately. Their shared generation cannot be proven; this board does not label the result live.";
    alert.hidden=false;
    dot.style.background="var(--amber)";
    return;
  }
  health.textContent="Connecting";
  alert.hidden=true;
  alert.textContent="";
}

function applyBoardDocuments(result){
  const first=!STATE.snapshot;
  STATE.readMeta=result.meta;
  STATE.readFailure="";
  STATE.pulse=result.pulse||null;
  render(result.snapshot,result.graph,result.timeline,result.bundleProjection);
  renderAttention();
  renderComms();
  setView(STATE.view);
  applyFilter();
  renderDetail();
  updateAreaLinks();
  renderFilterPanel();
  // A shared link carries the population it was taken with. Re-ask only on
  // initial hydration; background refreshes must not replay the same query.
  if(first&&STATE.pendingSemantic)restoreSemanticToken(STATE.pendingSemantic);
  renderBoardReadState();
}

async function refreshBoard(){
  const sequence=++STATE.readSequence;
  if(STATE.readController)STATE.readController.abort();
  if(STATE.readTimer)clearTimeout(STATE.readTimer);
  const controller=new AbortController();
  STATE.readController=controller;
  const timeout=setTimeout(()=>controller.abort("board read timeout"),BOARD_REQUEST_TIMEOUT_MS);
  try{
    const result=await readBoardDocuments(controller.signal);
    if(sequence!==STATE.readSequence)return;
    applyBoardDocuments(result);
  }catch(error){
    if(sequence!==STATE.readSequence)return;
    STATE.readFailure=error?.name==="AbortError"?"request timed out":String(error?.message||error||"request failed");
    renderBoardReadState();
    if(!STATE.snapshot)renderLocation();
  }finally{
    clearTimeout(timeout);
    if(sequence===STATE.readSequence){
      STATE.readController=null;
      STATE.readTimer=setTimeout(refreshBoard,BOARD_REFRESH_MS);
    }
  }
}


const SYSTEM_METRIC_PREFS_KEY="coord.system-telemetry.metrics.v1";
let LAST_SYSTEM_TELEMETRY=null;
function systemMetricPrefs(){try{return {...{cpu:true,gpu:true,memory:true,disk:true},...JSON.parse(localStorage.getItem(SYSTEM_METRIC_PREFS_KEY)||"{}")};}catch{return {cpu:true,gpu:true,memory:true,disk:true};}}
function renderSystemTelemetry(data){LAST_SYSTEM_TELEMETRY=data;window.CoordUsageDashboard?.setSystemTelemetry(data,systemMetricPrefs());}
if(typeof window!=="undefined"){window.CoordSystemTelemetryPreferences={get:systemMetricPrefs,set(key,enabled){const next=systemMetricPrefs();next[key]=enabled;try{localStorage.setItem(SYSTEM_METRIC_PREFS_KEY,JSON.stringify(next));}catch{}renderSystemTelemetry(LAST_SYSTEM_TELEMETRY);}};}
async function loadSystemTelemetry(){if(document.visibilityState!=="visible")return;try{const response=await fetch("/api/v1/system-telemetry?demand=1",{cache:"no-store"});renderSystemTelemetry(response.ok?await response.json():null);}catch{renderSystemTelemetry(null);}}
function startBoard(){
  readCapsule();
  const find=document.querySelector("#find");
  find.value=STATE.query;

  // One handler for both lists: a row is a row wherever it is drawn.
  document.querySelector(".split").addEventListener("click",event=>{
    const expand=event.target.closest("[data-expand-group]");
    if(expand){EXPANDED_GROUPS.add(expand.dataset.expandGroup);renderWorkTable();return;}
    const element=event.target.closest("[data-row]");
    if(!element)return;
    selectRow(element.dataset.row===STATE.selected?"":element.dataset.row);
  });
  storeDisplay();
  document.querySelector("#workmodes").addEventListener("click",event=>{
    const button=event.target.closest("[data-work-layout]");if(button)setWorkLayout(button.dataset.workLayout);
  });

  find.addEventListener("input",()=>{STATE.query=find.value;writeCapsule();applyFilter();});

  loadSavedViews();
  const displayButton=document.querySelector("#displaybtn");
  const displayPanel=document.querySelector("#displaypanel");
  const filterButton=document.querySelector("#filterbtn");
  const filterPanel=document.querySelector("#filterpanel");
  const renderDisplayPanel=()=>{
    displayPanel.innerHTML=`
      <div class="fgroup"><span class="flabel">Sort</span>${[["source","Source order"],["priority","Priority high→low"],["title","Title A→Z"],["status","Status"]].map(([value,label])=>
        `<button class="fchip" type="button" data-sort-opt="${value}" aria-pressed="${DISPLAY.sort===value}">${label}</button>`).join("")}</div>
      <div class="fgroup"><span class="flabel">Density</span>${["comfortable","compact"].map(value=>
        `<button class="fchip" type="button" data-density-opt="${value}" aria-pressed="${DISPLAY.density===value}">${value}</button>`).join("")}</div>
      <div class="fgroup"><span class="flabel">Group by</span>${["status","module","owner"].map(value=>{
        const disabled=DISPLAY.layout==="timeline"||(DISPLAY.layout==="board"&&value!=="status");
        const title=DISPLAY.layout==="timeline"?"Timeline is grouped by recorded UTC day":"Board lanes are grouped by status";
        return `<button class="fchip" type="button" data-group-opt="${value}" aria-pressed="${DISPLAY.group===value}"${disabled?` disabled title="${title}"`:""}>${value}</button>`;}).join("")}</div>
      <p class="dnote">Sort, density and grouping never change semantic membership. The current Work layout is kept in the shared URL. Board has no drag-to-status; Timeline invents no times.</p>`;
  };
  const setTransientPanel=active=>{
    const filterOpen=active==="filter";
    const displayOpen=active==="display";
    filterPanel.hidden=!filterOpen;
    displayPanel.hidden=!displayOpen;
    filterButton.setAttribute("aria-expanded",String(filterOpen));
    displayButton.setAttribute("aria-expanded",String(displayOpen));
    if(filterOpen)renderFilterPanel();
    if(displayOpen)renderDisplayPanel();
  };
  const closeTransientPanels=()=>{
    if(filterPanel.hidden&&displayPanel.hidden)return false;
    setTransientPanel("");
    return true;
  };
  displayButton.addEventListener("click",()=>{
    setTransientPanel(displayPanel.hidden?"display":"");
  });
  displayPanel.addEventListener("click",event=>{
    const sort=event.target.closest("[data-sort-opt]");
    if(sort){DISPLAY.sort=sort.dataset.sortOpt;storeDisplay();renderDisplayPanel();renderWorkTable();return;}
    const density=event.target.closest("[data-density-opt]");
    if(density){DISPLAY.density=density.dataset.densityOpt;storeDisplay();renderDisplayPanel();return;}
    const group=event.target.closest("[data-group-opt]");
    if(group){
      DISPLAY.group=group.dataset.groupOpt;
      EXPANDED_GROUPS.clear();   // expansions belong to the grouping they were made in
      storeDisplay();renderDisplayPanel();renderWorkTable();
    }
  });
  filterButton.addEventListener("click",()=>{
    setTransientPanel(filterPanel.hidden?"filter":"");
  });
  filterPanel.addEventListener("click",event=>{
    const drop=event.target.closest("[data-drop]");
    if(drop){
      event.stopPropagation();
      SEM.views.splice(Number(drop.dataset.drop),1);
      storeSavedViews();renderFilterPanel();return;
    }
    const expand=event.target.closest("[data-expand]");
    if(expand){SEM.expanded.add(expand.dataset.expand);renderFilterPanel();return;}
    const chip=event.target.closest("[data-facet]");
    if(chip){
      const set=SEM.facets[chip.dataset.facet];
      if(set.has(chip.dataset.value))set.delete(chip.dataset.value);
      else set.add(chip.dataset.value);
      runSemanticQuery();return;
    }
    if(event.target.id==="fclear"){
      Object.values(SEM.facets).forEach(set=>set.clear());
      runSemanticQuery();return;
    }
    if(event.target.id==="fsave"){
      // A saved view stores the token, not the rows it matched. It is a
      // projection: reopening it re-asks the question against the board as it
      // is now, which is the only reading that cannot go stale.
      const name=Object.entries(SEM.facets)
        .filter(([,set])=>set.size).map(([field,set])=>`${field}:${[...set].join("|")}`).join(" ");
      if(!name)return;
      SEM.views=[{name,facets:Object.fromEntries(
        Object.entries(SEM.facets).map(([field,set])=>[field,[...set]]))},
        ...SEM.views.filter(view=>view.name!==name)];
      storeSavedViews();renderFilterPanel();return;
    }
    const view=event.target.closest("[data-view-index]");
    if(view){
      const saved=SEM.views[Number(view.dataset.viewIndex)];
      if(!saved)return;
      Object.keys(SEM.facets).forEach(field=>{
        SEM.facets[field]=new Set(saved.facets[field]||[]);
      });
      runSemanticQuery();
    }
  });

  // A capsule pasted into an already-open tab, and the back button, are the
  // same event to this board: the URL changed without the document reloading.
  // Without this the address bar and the screen disagree, which is worse than
  // not supporting the link at all.
  window.addEventListener("hashchange",()=>{
    readCapsule();
    find.value=STATE.query;
    if(STATE.snapshot){
      renderAttention();
      setView(STATE.view);
      applyFilter();
      renderDetail();
    }
  });

  const paletteInput=document.querySelector("#cmdk-input");
  paletteInput.addEventListener("input",refreshPalette);
  document.querySelector("#cmdk-close").addEventListener("click",closePalette);
  document.querySelector("#cmdk").addEventListener("click",event=>{
    const item=event.target.closest(".cmdk-item");
    if(!item){if(event.target.id==="cmdk")closePalette();return;}
    const chosen=CMDK.items[Number(item.dataset.idx)];
    if(chosen&&chosen.run){closePalette();chosen.run();}
  });

  document.addEventListener("keydown",event=>{
    if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==="k"){
      event.preventDefault();
      if(CMDK.open)closePalette();else openPalette();
      return;
    }
    if(CMDK.open){
      if(trapPaletteFocus(event))return;
      if(event.key==="Escape"){event.preventDefault();closePalette();return;}
      if(event.target===paletteInput&&(event.key==="ArrowDown"||event.key==="ArrowUp")){
        event.preventDefault();
        const step=event.key==="ArrowDown"?1:-1;
        if(CMDK.items.length)CMDK.sel=(CMDK.sel+step+CMDK.items.length)%CMDK.items.length;
        renderPalette();return;
      }
      if(event.target===paletteInput&&event.key==="Enter"){
        event.preventDefault();
        const chosen=CMDK.items[CMDK.sel];
        if(chosen&&chosen.run){closePalette();chosen.run();}
        return;
      }
      if(!document.querySelector("#cmdk").contains(event.target)){event.preventDefault();paletteInput.focus();}
      return;
    }
    const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
    if(event.key==="Escape"&&closeTransientPanels()){
      event.preventDefault();
      return;
    }
    const rowControl=event.target.closest?.('[data-row][role="button"]');
    if(rowControl&&(event.key==="Enter"||event.key===" ")){
      event.preventDefault();
      selectRow(rowControl.dataset.row);
      rowControl.scrollIntoView?.({block:"nearest"});
      return;
    }
    if(event.key==="/"&&!typing){event.preventDefault();find.focus();find.select();return;}
    if(event.key==="Escape"){
      if(typing){find.blur();return;}
      if(STATE.selected)selectRow("");
      return;
    }
    if(typing||event.metaKey||event.ctrlKey||event.altKey)return;
    if(!event.shiftKey&&event.key.toLowerCase()==="f"){
      event.preventDefault();
      setTransientPanel(filterPanel.hidden?"filter":"");
      return;
    }
    if(event.shiftKey&&event.key.toLowerCase()==="v"){
      event.preventDefault();
      setTransientPanel(displayPanel.hidden?"display":"");
      return;
    }
    if(STATE.view==="work"&&["1","2","3"].includes(event.key)){
      event.preventDefault();setWorkLayout({"1":"list","2":"board","3":"timeline"}[event.key]);return;
    }
    if(event.key==="j"){event.preventDefault();moveSelection(1);}
    if(event.key==="k"){event.preventDefault();moveSelection(-1);}
    if(event.key==="Enter"&&STATE.selected)renderDetail();
  });

  // Provider usage is an independent, fail-soft read. A slow or unavailable
  // upstream must never prevent the coordination board from becoming usable.
  if(window.CoordUsageDashboard){
    window.CoordUsageDashboard.load();
  }
  loadSystemTelemetry();
  window.setInterval(loadSystemTelemetry,5000);
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")loadSystemTelemetry();});
  refreshBoard();
}
if(typeof document!=="undefined"&&typeof fetch==="function")startBoard();
