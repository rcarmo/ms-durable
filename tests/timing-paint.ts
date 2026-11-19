/** Exercise the production renderer through its API; sample painted pixels at each distinct port. */
import type {Page} from 'playwright';

export async function timingPaint(page: Page, url: string, root: string, theme: string) {
  const gate = {index:0,requested:105,resolved:112,status:'approved',tool:'write'};
  const call = (index:number,start:number,first:number|null,end:number) => ({index,start:start-.2,end,response:end,request:{start,first_token:first,end},usage:{model_name:'Example model'}});
  const tool = (name:string,start:number,end:number,extra={}) => ({name,start,end,requested:102,call:1,returns_to:2,success:true,...extra});
  const turn = {id:'paint',number:1,received:97,sdk_received:100,kind:'prompt',prompt:'Parallel tools and a measured approval',status:'done',start:100,end:118,calls:[call(1,100,101,102),call(2,114.02,null,118)],tools:[tool('read',102.01,104),tool('inspect',102.02,114),tool('write',105,114.01,{approvals:[0],approval:gate}),tool('fast failure',102.03,102.031,{success:false})],approvals:[gate]};
  await page.route('**/api/sessions/*/timing*',r=>r.fulfill({json:{turns:[turn],more:false}}));
  await page.goto(url+'#token=synthetic-ui-token'); await page.locator('#timing-toggle').click();
  const chart = page.locator('.tt-chart svg.timing'); await chart.waitFor();
  const heading=page.locator('.tt-head'); await heading.hover();
  if(/SDK|before the .*prompt/i.test(await heading.innerText()+' '+await page.locator('#timing-tip').innerText()))throw Error('Turn timing implies an unmeasured prompt handoff');
  const scales=[6.25,12.5,25,50,100,200];
  for(const z of scales){
    while(parseFloat(await page.locator('#timing-scale').innerText())!==z){const current=parseFloat(await page.locator('#timing-scale').innerText());await page.locator(current>z?'#timing-zoom-out':'#timing-zoom-in').click();}
    // Capture the complete SVG at native scale, even when the real pane is scrollable.
    const width = await chart.evaluate(el=>el.getBoundingClientRect().width);
    await page.setViewportSize({width:Math.max(1250,Math.ceil(width)+520),height:680}); await page.mouse.move(0,0);
    if(z===50) await page.locator('.tt-body').screenshot({path:`${root}/timing-detail-${theme}.png`});
    const png=(await chart.screenshot({path:`${root}/timing-${theme}-${z}.png`})).toString('base64');
    const result=await chart.evaluate(async (el, png)=>{
      const image=new Image();image.src='data:image/png;base64,'+png;await image.decode();
      const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;
      const ctx=canvas.getContext('2d')!;ctx.drawImage(image,0,0);const {data}=ctx.getImageData(0,0,image.width,image.height);
      const rgb=(x:number,y:number)=>{const i=4*(Math.floor(y)*image.width+Math.floor(x));return [...data.slice(i,i+3)];};
      const distance=(a:number[],b:number[])=>Math.max(...a.map((v,i)=>Math.abs(v-b[i])));
      const rects=[...el.querySelectorAll<SVGRectElement>('.bar rect')].map(r=>r.getBBox());
      const paths=[...el.querySelectorAll<SVGPathElement>('.dep')]; const issues:string[]=[];
      const band=el.querySelector<SVGGraphicsElement>('.brk')!.getBBox();
      const toolBars=[...el.querySelectorAll<SVGRectElement>('rect.tool')].map(r=>r.getBBox());
      if(toolBars.length!==4 || toolBars[2].x < band.x+band.width-.01 || !(toolBars[1].x<band.x && toolBars[1].x+toolBars[1].width>band.x+band.width))issues.push('gated or concurrent tool painted incorrectly');
      if(!el.querySelector('.bar .unknown') || !el.querySelector('.tool.fail'))issues.push('unknown phase or failed tool missing');
      for(const path of paths){const length=path.getTotalLength(),p=path.getPointAtLength(length),q=path.getPointAtLength(length-9),start=path.getPointAtLength(0);
        if(Math.abs(q.y-p.y)>.05 || Math.abs(p.x-q.x-9)>.05 || !rects.some(r=>Math.abs(p.x-r.x)<.05&&p.y>r.y&&p.y<r.y+r.height) || !rects.some(r=>Math.abs(start.x-r.x-r.width)<.05&&start.y>r.y&&start.y<r.y+r.height))issues.push('port missed');
        for(const r of rects){const y=r.y+r.height/2;if(y<=start.y||y>=p.y)continue;
          for(let t=0;t<length;t+=2){const m=path.getPointAtLength(t);if(m.y>r.y&&m.y<r.y+r.height&&m.x>r.x&&m.x<r.x+r.width){issues.push('shaft behind unrelated bar');break;}}
        }
        // Two pixels behind the tip the triangle should be ink, not row background.
        if(distance(rgb(p.x-3,p.y),rgb(p.x-3,p.y-4))<20)issues.push('unpainted arrowhead');
      }
      if(paths.length!==8)issues.push('missing dependency');return issues;
    },png);
    if(result.length)throw Error(`${theme} ${z} px/s: ${result.join(', ')}`);
  }
  // One band spans two overlapping approvals. Success and failure without an
  // approval, and work after an earlier answer, can all finish inside it.
  const second={...gate,index:1,requested:107,resolved:114};
  const parallel={...turn,approvals:[gate,second],tools:[tool('read',104,108),
    tool('failed lookup',113.998,113.999,{success:false}),tool('early answer',105,113,{approvals:[0]}),
    tool('last answer',107,115,{approvals:[1]})]};
  await page.route('**/api/sessions/*/timing*',r=>r.fulfill({json:{turns:[parallel],more:false}}));
  await page.locator('#timing-close').click(); await page.locator('#timing-toggle').click();
  await page.locator('.tt-labels').getByText('early answer',{exact:true}).waitFor();
  for(const z of [...scales].reverse()){
    while(parseFloat(await page.locator('#timing-scale').innerText())!==z)await page.locator('#timing-zoom-out').click();
    if(!await chart.evaluate(el=>{
      const b=el.querySelector<SVGGraphicsElement>('.brk')!.getBBox();
      const x=(t:number)=>b.x+b.width*(t-105)/9;
      const bars=[...el.querySelectorAll<SVGRectElement>('rect.tool')].map(r=>r.getBBox());
      return el.querySelectorAll('.brk').length===1 && bars.length===4 && [108,113.999,113].every((t,i)=>Math.abs(bars[i].x+bars[i].width-x(t))<.01)
        && Math.abs(bars[2].x-x(112))<.01 && Math.abs(bars[3].x-x(114))<.01;
    }))throw Error('Incorrect in-band completion at '+z+' px/s');
  }
  while(parseFloat(await page.locator('#timing-scale').innerText())<200)await page.locator('#timing-zoom-in').click();
  await page.locator('.tt-head').hover();
  if (!(await page.locator('#timing-tip').innerText()).includes('9.00 s of recorded wait')) throw Error('Overlapping waits counted twice');
  // Reuse the production grouping function: chained and nested waits, an unknown answer
  // inside a known wait, a disjoint interval, and an open interval as of this frame.
  const intervals = await page.evaluate(() => {
    const g=(requested,resolved,status='approved')=>({requested,resolved,status});
    return approvalBands([g(10,15),g(11,12),g(14,18),g(16,null),g(20,null,'pending'),g(21,22)],24)
      .map(b=>[b.t,b.wait,b.items.length]);
  });
  if(JSON.stringify(intervals)!==JSON.stringify([[10,8,4],[20,4,2]]))throw Error('Incorrect overlap intervals: '+JSON.stringify(intervals));
  // Historical approval: an unrelated tool failed 9 ms before / 14 ms after the request.
  const historical={...turn,approvals:[{...gate,resolved:null}],tools:[
    tool('crossing',104.991,105.014,{success:false}),tool('before',104.995,104.999,{success:false}),
    tool('long crossing',103,106,{success:false}),tool('write',105,114,{approvals:[0]})]};
  await page.route('**/api/sessions/*/timing*',r=>r.fulfill({json:{turns:[historical],more:false}}));
  await page.locator('#timing-close').click(); await page.locator('#timing-toggle').click();
  await page.locator('.tt-labels').getByText('long crossing',{exact:true}).waitFor();
  for(const z of [...scales].reverse()){
    while(parseFloat(await page.locator('#timing-scale').innerText())!==z)await page.locator('#timing-zoom-out').click();
    const ok=await chart.evaluate(el=>{
      const b=el.querySelector<SVGGraphicsElement>('.brk')!.getBBox();
      const rows=[...el.querySelectorAll('.bar')].filter(g=>g.querySelector('rect.fail')).map(g=>[...g.querySelectorAll<SVGRectElement>('rect.fail')].map(r=>r.getBBox()));
      return rows.length===3 && rows[0].length===1 && rows[0][0].x>=b.x+b.width-.01 && rows[0][0].width>=2.99 &&
        rows[1].length===1 && rows[1][0].x+rows[1][0].width<=b.x+.01 && rows[1][0].width>=2.99 && rows[2].length===2;
    });
    if(!ok)throw Error(`${theme} ${z} px/s: historical approval splits or clips a short failed tool`);
  }
  await page.locator('#timing-zoom-in').click(); await page.locator('#timing-zoom-in').click(); await page.locator('#timing-zoom-in').click();
  await page.setViewportSize({width:390,height:780});
  const layout=await page.locator('.tt-chart').evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth,page:document.documentElement.scrollWidth}));
  if(layout.width<150 || layout.scroll<=layout.width || layout.page>390)throw Error('Timing chart does not scroll within mobile pane: '+JSON.stringify(layout));
}
