import {test,expect} from '@playwright/test';

test('overview discovers grouped apps, navigation and scan work',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/');
  await expect(page.getByRole('heading',{name:'Overview',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Finance Vault',exact:true})).toBeVisible({timeout:20000});
  await expect(page.locator('.app-card').filter({has:page.getByRole('heading',{name:'Finance Vault',exact:true})})).toHaveCount(1);
  await page.getByRole('button',{name:'Scan for apps',exact:true}).click();
  await expect(page.getByRole('status').filter({hasText:'discovered'})).toBeVisible({timeout:30000});
  await page.getByRole('link',{name:'Finance Vault',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Associated containers'})).toBeVisible();
  await expect(page.getByRole('link',{name:'Open app',exact:true})).toHaveAttribute('href',/\.ts\.net:8444/);
  await expect(page.getByRole('heading',{name:'Recent logs'})).toBeVisible();
  await page.getByRole('link',{name:'Processes',exact:true}).click();
  await expect(page.locator('tbody tr').first()).toBeVisible();
  await page.getByRole('link',{name:'Activity',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Activity',exact:true})).toBeVisible();
  expect(errors).toEqual([]);
});

test('Android layout fits and installation assets are valid',async({page,request})=>{
  await page.setViewportSize({width:393,height:851});await page.goto('/');
  await expect(page.getByRole('button',{name:'Install app'})).toBeVisible();
  await page.getByRole('button',{name:'Install app'}).click();
  // Chromium may show a native install prompt or the in-page Android instructions.
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  const manifest=await request.get('/manifest.webmanifest');expect(manifest.headers()['content-type']).toContain('application/manifest+json');
  const data=await manifest.json();expect(data.display).toBe('standalone');expect(data.start_url).toBe('/');
  for(const icon of data.icons){const r=await request.get(icon.src);expect(r.headers()['content-type']).toContain('image/png');expect((await r.body()).length).toBeGreaterThan(1000);}
  await page.evaluate(()=>navigator.serviceWorker.ready);
  await expect.poll(()=>page.evaluate(()=>!!navigator.serviceWorker.controller)).toBeTruthy();
  await page.screenshot({path:'test-results/mobile.png',fullPage:true});
});

test('offline fallback stores no administrative data',async({page,context})=>{
  await page.goto('/');await page.evaluate(()=>navigator.serviceWorker.ready);
  await expect.poll(()=>page.evaluate(()=>!!navigator.serviceWorker.controller)).toBeTruthy();
  const cached=await page.evaluate(async()=>{const keys=await caches.keys();return (await Promise.all(keys.map(async key=>(await(await caches.open(key)).keys()).map(r=>r.url)))).flat();});
  expect(cached.every(url=>!url.includes('/api/'))).toBeTruthy();
  await context.setOffline(true);await page.goto('/apps');
  await expect(page.getByRole('heading',{name:'Your Pi is out of reach.'})).toBeVisible();
  await context.setOffline(false);
});

test('desktop screenshot and keyboard-friendly registration dialog',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});await page.goto('/');
  await expect(page.getByRole('heading',{name:'Finance Vault',exact:true})).toBeVisible();
  await page.screenshot({path:'test-results/desktop.png',fullPage:true});
  await page.getByRole('button',{name:'Register app'}).click();
  await expect(page.getByRole('dialog')).toBeVisible();await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
