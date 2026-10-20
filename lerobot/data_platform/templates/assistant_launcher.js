// One shared contextual panel; the server resolves every referenced object and permission.
window.openPlatformAssistant = ({locationId, type = 'dataset', targetId = locationId}) => {
  if (!locationId || !targetId) return;
  document.getElementById('platform-assistant-dialog')?.remove();
  const dialog = document.createElement('dialog'); dialog.id = 'platform-assistant-dialog';
  dialog.style.cssText = 'position:fixed;inset:0 0 0 auto;margin:0;height:100dvh;width:min(580px,100vw);max-width:100vw;max-height:100dvh;box-sizing:border-box;background:#020617;color:#e2e8f0;border:1px solid #334155;padding:12px';
  const close = document.createElement('button'); close.textContent = 'Close assistant'; close.onclick = () => {dialog.close(); dialog.remove();};
  const frame = document.createElement('iframe'); frame.title = 'Contextual AI assistant'; frame.style.cssText = 'display:block;width:100%;height:calc(100% - 40px);border:0';
  frame.src = '/assistant?' + new URLSearchParams({location_id:locationId,type,target_id:targetId});
  dialog.append(close,frame); document.body.append(dialog); dialog.addEventListener('cancel',()=>dialog.remove()); dialog.showModal();
};
window.closePlatformAssistant = () => document.getElementById('platform-assistant-dialog')?.remove();
