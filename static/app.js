document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('form[method="post"],form[method="POST"]').forEach(form=>{if(window.CSRF_TOKEN&&!form.querySelector('input[name="_csrf"]')){const field=document.createElement('input');field.type='hidden';field.name='_csrf';field.value=window.CSRF_TOKEN;form.appendChild(field)}})});
function openForm(id){const row=document.getElementById(id);if(row){row.classList.add('show');row.scrollIntoView({behavior:'smooth',block:'center'});const input=row.querySelector('input:not([type=hidden])');if(input)input.focus()}}
function closeForm(id){const row=document.getElementById(id);if(row)row.classList.remove('show')}
function openModal(id){const modal=document.getElementById('modal');const content=document.getElementById('modal-content');const source=document.getElementById(id);if(modal&&source){content.innerHTML=source.innerHTML;modal.style.display='flex'}}
function closeModal(){const modal=document.getElementById('modal');if(modal)modal.style.display='none'}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
