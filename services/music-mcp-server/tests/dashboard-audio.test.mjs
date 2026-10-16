import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const src=fs.readFileSync(new URL('../verdantflare_music_mcp/dashboard_src/dashboard.mjs',import.meta.url),'utf8');
function setup(api){
 const audio={src:'',paused:true,removeAttribute(){this.src=''},pause(){this.paused=true},async play(){this.paused=false}};
 const nodes=new Map([['#audioElement',audio]]);
 const get=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',style:{},dataset:{},disabled:false});return nodes.get(id)};
 const stem={artifact_id:'a',name:'audio.wav',media_type:'audio/wav'};
 const ctx={$:get,apiRequest:api,previewCache:new Map(),isPageActive:true,currentDetailTask:{stems:[stem]},activeStemIdx:0,window:{}};
 vm.createContext(ctx);vm.runInContext(src.slice(src.indexOf('let audioRevision ='),src.indexOf('async function hydrateRefAudioPreviews')),ctx);ctx.selectAudioStem(stem);
 return {ctx,audio,get,stem};
}
test('play loads once, then plays; changing stem rejects a late response',async()=>{
 let calls=0,finish;const {ctx,audio,stem}=setup(()=>{calls++;return new Promise(r=>finish=r)});
 const first=ctx.toggleAudioPlayback();await ctx.toggleAudioPlayback();assert.equal(calls,1);finish({data_url:'data:audio/wav;base64,AA=='});await first;assert.equal(audio.paused,false);
 ctx.selectAudioStem({...stem,artifact_id:'b'});ctx.currentDetailTask.stems[0]={...stem,artifact_id:'b'};
 const pending=ctx.toggleAudioPlayback();ctx.selectAudioStem({...stem,artifact_id:'c'});finish({data_url:'data:audio/wav;base64,AQ=='});await pending;assert.equal(audio.src,'');assert.equal(audio.paused,true);
});
test('failed preview stays failed and retry can recover',async()=>{
 let fail=true;const {ctx,audio,get}=setup(async()=>{if(fail)throw Error('offline');return {data_url:'data:audio/wav;base64,AA=='}});
 await ctx.toggleAudioPlayback();assert.match(get('#playerStatusText').textContent,/加载失败.*offline/);assert.equal(audio.src,'');fail=false;await ctx.toggleAudioPlayback();assert.equal(audio.paused,false);
});
