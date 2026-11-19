/** Exercise the production renderer through its API; sample painted pixels at each distinct port. */
import type {Page} from 'playwright';

export async function timingPaint(page: Page, url: string, root: string, theme: string) {
  const gate = {index:0,requested:105,resolved:112,status:'approved',tool:'write'};
  const call = (index:number,start:number,first:number|null,end:number) => ({index,start:start-.2,end,response:end,request:{start,first_token:first,end},usage:{model_name:'Example model'}});
  const tool = (name:string,start:number,end:number,extra={}) => ({name,start,end,requested:102,call:1,returns_to:2,success:true,...extra});
  const turn = {id:'paint',number:1,kind:'prompt',prompt:'Parallel tools and a measured approval',status:'done',start:100,end:118,calls:[call(1,100,101,102),call(2,114.02,null,118)],tools:[tool('read',102.01,104),tool('inspect',102.02,114),tool('write',105,114.01,{approvals:[0],approval:gate}),tool('fast failure',102.03,102.031,{success:false})],approvals:[gate]};
  await page.route('**/api/sessions/*/timing*',r=>r.fulfill({json:{turns:[turn],more:false}}));
  await page.goto(url+'#token=synthetic-ui-token'); await page.locator('#timing-toggle').click();
  const chart = page.locator('.tt-chart svg.timing'); await chart.waitFor();
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
  await page.setViewportSize({width:390,height:780});
  const layout=await page.locator('.tt-chart').evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth,page:document.documentElement.scrollWidth}));
  if(layout.width<150 || layout.scroll<=layout.width || layout.page>390)throw Error('Timing chart does not scroll within mobile pane: '+JSON.stringify(layout));
}
