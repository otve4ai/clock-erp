(() => {
    const $ = id => document.getElementById(id);
    const api = '/api/v1/receipts/transfers';
    const statuses = {draft:'Черновик',in_transit:'В пути',received:'Принято',cancelled:'Отменено'};
    let items = [], key = crypto.randomUUID(), timer, version = 0;
    const message = text => $('transfer-message').textContent = text;
    async function request(path, payload, requestKey) {
        const response = await fetch(api + path, {method:payload ? 'POST' : 'GET',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content,'Idempotency-Key':requestKey || ''},body:payload ? JSON.stringify(payload) : undefined});
        const result = await response.json();
        if (!response.ok || !result.ok) throw Error(result.message || 'Не удалось выполнить операцию');
        return result.data;
    }
    function node(tag, text) {const el=document.createElement(tag);el.textContent=text;return el;}
    async function refresh() {
        const docs = await request(''); $('transfer-list').replaceChildren();
        for (const doc of docs) {
            const tr=node('tr','');tr.append(node('td',doc.number),node('td',`${doc.from_name} → ${doc.to_name}\n${doc.items.map(i=>i.name+' × '+i.quantity).join('; ')}`),node('td',statuses[doc.status]));
            const actions=node('td','');
            for (const [action,label] of (doc.status==='draft' ? [['send','Отправить'],['cancel','Отменить']] : doc.status==='in_transit' ? [['receive','Подтвердить приёмку']] : [])) {
                const button=node('button',label);button.type='button';
                button.onclick=async()=>{button.disabled=true;try{await request(`/${doc.id}/${action}`,{});await refresh();message('Операция выполнена');}catch(e){message(e.message);button.disabled=false;}};
                actions.append(button);
            }
            tr.append(actions);$('transfer-list').append(tr);
        }
    }
    async function search() {
        const current=++version;
        try {const products=await request('/products?'+new URLSearchParams({q:$('transfer-search').value,warehouse_id:$('transfer-from').value}));if(current!==version)return;
            $('transfer-product').replaceChildren(new Option('Выберите товар',''));
            for(const p of products)$('transfer-product').append(new Option(`${p.name} · ${p.article||''} · ${p.stock} шт.`,p.id));
        }catch(e){message(e.message);}
    }
    function showItems(){ $('transfer-items').replaceChildren(); items.forEach((item,index)=>{const li=node('li',`${item.name} × ${item.quantity} `),remove=node('button','Убрать');remove.type='button';remove.onclick=()=>{items.splice(index,1);key=crypto.randomUUID();showItems();};li.append(remove);$('transfer-items').append(li);});}
    $('transfer-add').onclick=()=>{const select=$('transfer-product'),quantity=Number($('transfer-quantity').value);if(!select.value||!Number.isInteger(quantity)||quantity<=0){message('Выберите товар и целое положительное количество');return;}if(items.some(i=>i.product_id===select.value)){message('Товар уже добавлен');return;}items.push({product_id:select.value,quantity,name:select.selectedOptions[0].textContent});key=crypto.randomUUID();showItems();};
    $('transfer-search').oninput=()=>{clearTimeout(timer);timer=setTimeout(search,250);};
    $('transfer-from').onchange=()=>{items=[];key=crypto.randomUUID();showItems();search();};
    for(const id of ['transfer-to','transfer-comment']) $(id).addEventListener('change',()=>key=crypto.randomUUID());
    $('transfer-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await request('',{from_warehouse_id:$('transfer-from').value,to_warehouse_id:$('transfer-to').value,items:items.map(({product_id,quantity})=>({product_id,quantity})),comment:$('transfer-comment').value},key);items=[];key=crypto.randomUUID();showItems();await refresh();message('Черновик создан. Остатки изменятся после отправки.');}catch(e){message(e.message);}finally{button.disabled=false;}};
    refresh().catch(e=>message(e.message));search();
})();
