import pkg from 'playwright'; const { chromium } = pkg;
const b=await chromium.launch();
const p=await b.newPage({viewport:{width:1280,height:720},deviceScaleFactor:1});
await p.goto('file://'+import.meta.dirname+'/thumb.html',{waitUntil:'load'});
await p.evaluate(()=>document.fonts.ready); await p.waitForTimeout(400);
await p.screenshot({path:import.meta.dirname+'/../og-image.png'});
await b.close();
