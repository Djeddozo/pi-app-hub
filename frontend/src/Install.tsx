import { useEffect, useState } from 'react';
import { ArrowDownToLine, X } from 'lucide-react';
import './install.css';
type InstallPrompt = Event & { prompt: () => Promise<void>; userChoice: Promise<{outcome:string}> };
export function Install() {
  const [prompt,setPrompt]=useState<InstallPrompt|null>(null), [help,setHelp]=useState(false), [installed,setInstalled]=useState(window.matchMedia('(display-mode: standalone)').matches);
  useEffect(()=>{const ready=(e:Event)=>{e.preventDefault();setPrompt(e as InstallPrompt);};const done=()=>{setInstalled(true);setPrompt(null);};window.addEventListener('beforeinstallprompt',ready);window.addEventListener('appinstalled',done);return()=>{window.removeEventListener('beforeinstallprompt',ready);window.removeEventListener('appinstalled',done);};},[]);
  async function install(){if(prompt){await prompt.prompt();await prompt.userChoice;setPrompt(null);}else setHelp(!help);}
  if(installed)return null;
  return <div className="install"><button className="quiet" onClick={()=>void install()}><ArrowDownToLine size={14}/><span>Install app</span></button>{help && <div className="install-help" role="status"><button className="icon-button" aria-label="Close install instructions" onClick={()=>setHelp(false)}><X size={16}/></button><strong>Pi App Hub on your home screen</strong><p>On Android, open this private HTTPS address in Chrome, then choose <b>⋮ → Add to Home screen → Install</b>.</p><p>Keep Tailscale connected to reach your Pi. If Chrome offers a shortcut instead, reload and try again once the page is fully loaded.</p></div>}</div>;
}
