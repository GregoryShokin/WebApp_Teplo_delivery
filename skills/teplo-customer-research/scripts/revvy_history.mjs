// SignalR JSON protocol for Revvy's observed read-only history flow.
// Input on stdin: {token, path}; never pass a token in argv or print the socket URL.
import fs from 'node:fs';
const {token, path} = JSON.parse(fs.readFileSync(0, 'utf8'));
const parsed = new URL(path, 'https://revvy.ru');
if (parsed.origin !== 'https://revvy.ru' || parsed.pathname !== '/web/adminChat/messages') throw new Error('Invalid history route');
const chatId = parsed.searchParams.get('chatId');
if (!chatId || !parsed.searchParams.has('messageChannel')) throw new Error('chatId and messageChannel required');
const ws = new WebSocket('wss://revvy.ru/hubs/admin_chat?access_token=' + encodeURIComponent(token));
let buffer = '', requested = false, finished = false, triggerOk = false, pendingMessages = null;
const timer = setTimeout(() => finish(new Error('SignalR history timed out')), 55000);
function finish(error, data) {
  if (finished) return;
  finished = true; clearTimeout(timer); ws.close();
  if (error) {process.stderr.write(error.message + '\n'); process.exitCode=1;}
  else process.stdout.write(JSON.stringify(data));
}
ws.addEventListener('error', () => finish(new Error('SignalR connection failed')));
ws.addEventListener('close', () => {if (!finished) finish(new Error('SignalR closed before history arrived'));});
ws.addEventListener('open', () => ws.send(JSON.stringify({protocol:'json',version:1})+'\u001e'));
ws.addEventListener('message', async ({data}) => {
  try {
    buffer += typeof data === 'string' ? data : Buffer.from(await data.arrayBuffer()).toString('utf8');
    const frames = buffer.split('\u001e'); buffer = frames.pop();
    for (const frame of frames) {
      if (!frame) continue;
      const event = JSON.parse(frame);
      if (event.error) return finish(new Error('SignalR handshake rejected'));
      if (!requested && !('type' in event)) {
        requested = true;
        const response = await fetch(parsed, {headers:{Authorization:'Bearer '+token,'Accept-Language':'ru-RU'}, signal:AbortSignal.timeout(30000)});
        if (!response.ok) return finish(new Error('History trigger HTTP '+response.status));
        await response.arrayBuffer();
        triggerOk = true;
        if (pendingMessages) return finish(null, pendingMessages);
      } else if (event.type === 1 && event.target?.toLowerCase() === 'sendmessagestorevvyfront') {
        let messages = event.arguments?.[0];
        if (typeof messages === 'string') messages = JSON.parse(messages);
        if (!Array.isArray(messages)) return finish(new Error('Unexpected history schema'));
        const matches = messages.filter(m => m.chatId === chatId);
        if (matches.length) {
          pendingMessages = matches;
          if (triggerOk) return finish(null, pendingMessages);
          continue;
        }
        if (!messages.length) return finish(new Error('Empty event without chat identity; completeness unconfirmed'));
      } else if (event.type === 7) return finish(new Error('SignalR server ended connection'));
    }
  } catch (error) {finish(new Error('History read failed: '+error.name));}
});
