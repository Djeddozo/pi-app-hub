import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./tests', workers:1, use:{baseURL:process.env.HUB_URL || 'http://127.0.0.1:8840', launchOptions:{executablePath:'/usr/bin/chromium',args:['--no-sandbox']}}, reporter:'list'});
