import { chromium } from '@playwright/test';
import { readFile } from 'node:fs/promises';
const browser = await chromium.launch({executablePath:'/usr/bin/chromium', args:['--no-sandbox']});
try {
  const page = await browser.newPage({deviceScaleFactor:1});
  const svg = await readFile(new URL('./public/icon.svg', import.meta.url), 'utf8');
  for (const size of [192,512]) {
    await page.setViewportSize({width:size,height:size});
    await page.setContent(`<style>body{margin:0}svg{width:100vw;height:100vh;display:block}</style>${svg}`);
    await page.screenshot({path:new URL(`./public/icon-${size}.png`, import.meta.url).pathname});
  }
} finally {await browser.close();}
