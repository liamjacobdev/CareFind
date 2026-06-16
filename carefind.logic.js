/* CareFind — pure presentation/transform logic, extracted from carefind.html so it
 * can be unit-tested in Node (Vitest) without a browser. This is the SINGLE source
 * of truth for these functions; carefind.html loads this file and uses them as
 * globals (no duplicated definitions to drift).
 *
 * UMD wrapper: in Node it's `module.exports`; in the browser it attaches the same
 * names to `window` as plain globals — so a classic <script src> works (and still
 * loads over file://, unlike an ES module). Keep everything here PURE: no DOM, no
 * `state`, no network. Anything needing page state takes it as an argument.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;   // Node / Vitest
  else Object.assign(root, api);                                            // browser globals
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Specialty label -> NPPES taxonomy_description (real taxonomy text).
  const TAXONOMY_MAP = {
    'Family Medicine':'Family Medicine','Internal Medicine':'Internal Medicine','Pediatrics':'Pediatrics',
    'Cardiology':'Cardiovascular Disease','Orthopaedic':'Orthopaedic Surgery','Dermatology':'Dermatology',
    'Obstetrics':'Obstetrics & Gynecology','Psychiatry':'Psychiatry','Neurology':'Neurology',
    'Hematology & Oncology':'Hematology & Oncology','Ophthalmology':'Ophthalmology','Urology':'Urology',
    'Endocrinology':'Endocrinology, Diabetes & Metabolism','Gastroenterology':'Gastroenterology',
    'Dentist':'Dentist','Physical Therapist':'Physical Therapist','Nurse Practitioner':'Nurse Practitioner',
    'Chiropractor':'Chiropractor',
  };
  const PALETTE = ['#0f7a5f','#2563a8','#7c5ce0','#a8551e','#b8344f','#3f7d3a','#0e6f8a','#9a5ab0','#92681a','#1f8a6d'];

  function toTitleCase(s){ if(!s) return ''; return s.toLowerCase().replace(/\b([a-z])/g,c=>c.toUpperCase()).replace(/\b(Ii|Iii|Iv|Md|Do|Pa|Np|Dds|Dpm|Llc|Pllc|Pc|Pa-C|Dnp|Rn)\b/g,m=>m.toUpperCase()); }
  function formatPhone(p){ const d=String(p).replace(/\D/g,''); if(d.length===10) return `(${d.slice(0,3)}) ${d.slice(3,6)}-${d.slice(6)}`; if(d.length===11&&d[0]==='1') return `(${d.slice(1,4)}) ${d.slice(4,7)}-${d.slice(7)}`; return p||''; }
  function hashStr(s){ let h=5381; for(let i=0;i<s.length;i++) h=((h<<5)+h+s.charCodeAt(i))>>>0; return h; }
  function haversine(a,b){ const R=3958.8,toR=x=>x*Math.PI/180; const dLat=toR(b[0]-a[0]),dLng=toR(b[1]-a[1]); const x=Math.sin(dLat/2)**2+Math.cos(toR(a[0]))*Math.cos(toR(b[0]))*Math.sin(dLng/2)**2; return 2*R*Math.asin(Math.sqrt(x)); }
  function cssEsc(s){ return String(s).replace(/["\\]/g,'\\$&'); }
  function esc(s){ if(s===0) return '0'; if(!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

  function buildNpiParams(f){
    const p=new URLSearchParams({version:'2.1',limit:f.limit,skip:'0'});
    if(f.npi){ p.set('number',f.npi); return p; }
    // Radius >10mi widens beyond the exact ZIP via a 3-digit prefix wildcard;
    // geocodeProviders() then distance-filters the geocoded results.
    if(f.zip) p.set('postal_code',(parseInt(f.radius,10)>10 && f.zip.length===5)?f.zip.slice(0,3)+'*':f.zip);
    if(f.city) p.set('city',f.city);
    if(f.st) p.set('state',f.st);
    if(f.zip||f.city) p.set('address_purpose','LOCATION');
    if(f.specialty && TAXONOMY_MAP[f.specialty]) p.set('taxonomy_description',TAXONOMY_MAP[f.specialty]);
    if(f.type) p.set('enumeration_type',f.type);
    if(f.name){
      const wild=s=>s.replace(/\*+$/,'')+'*';
      if(f.type==='NPI-2'){ p.set('organization_name',wild(f.name)); }
      else {
        const parts=f.name.split(/\s+/);
        if(parts.length>1){ p.set('first_name',wild(parts[0])); p.set('last_name',wild(parts.slice(1).join(' '))); }
        else { p.set('last_name',wild(f.name)); }
      }
    }
    return p;
  }

  // Standalone (no-backend) path: mirrors the backend's normalize() (app/main.py)
  // field-for-field. Keep the two in sync if you add a field.
  function buildProviders(results){
    return results.map(r=>{
      const npi=String(r.number);
      const b=r.basic||{};
      const isOrg=r.enumeration_type==='NPI-2';
      let name, initials;
      if(isOrg){
        name=toTitleCase(b.organization_name||b.name||'Healthcare Organization');
        initials=name.replace(/[^A-Za-z ]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'OR';
      } else {
        const first=b.first_name||'', last=b.last_name||'';
        const cred=b.credential?`, ${b.credential.replace(/\.$/,'')}`:'';
        name=(toTitleCase(`${first} ${last}`.trim())+cred)||'Unnamed Provider';
        initials=`${(first[0]||'').toUpperCase()}${(last[0]||'').toUpperCase()}`||'DR';
      }
      const addrs=r.addresses||[];
      const loc=addrs.find(a=>a.address_purpose==='LOCATION')||addrs[0]||{};
      const mail=addrs.find(a=>a.address_purpose==='MAILING');
      const fmtAddr=a=>a?[toTitleCase([a.address_1,a.address_2].filter(Boolean).join(' ')),toTitleCase(a.city||''),a.state,(a.postal_code||'').substring(0,5)].filter(Boolean).join(', '):'';
      const taxes=(r.taxonomies||[]).map(t=>({desc:t.desc||'',code:t.code||'',primary:!!t.primary,state:t.state||'',license:t.license||''}));
      const primary=taxes.find(t=>t.primary)||taxes[0]||{desc:'Healthcare Provider'};
      const phoneRaw=(loc.telephone_number||'').replace(/\D/g,'');
      return {
        npi, name, initials, isOrg,
        specialty:primary.desc||'Healthcare Provider',
        taxonomies:taxes,
        address1:toTitleCase([loc.address_1,loc.address_2].filter(Boolean).join(' ')),
        city:toTitleCase(loc.city||''), stateAb:loc.state||'', postalCode:(loc.postal_code||'').substring(0,5),
        fullAddress:fmtAddr(loc), mailingAddress:mail?fmtAddr(mail):'',
        phone:formatPhone(loc.telephone_number||''), phoneRaw, fax:formatPhone(loc.fax_number||''),
        gender:b.gender==='M'?'Male':(b.gender==='F'?'Female':''),
        soleProprietor:b.sole_proprietor||'', credential:b.credential||'',
        status:b.status==='A'?'Active':(b.status||''),
        enumerationDate:b.enumeration_date||'', lastUpdated:b.last_updated||'',
        lat:null,lng:null,geocoded:false,distance:null,
        color:PALETTE[hashStr(primary.desc||npi)%PALETTE.length],
      };
    });
  }

  // Adapts the backend's normalized provider shape for the UI. `center` is the
  // search-center [lat,lng] (or null) — passed in rather than read from page state
  // so this stays pure; the caller supplies state.center.
  function adaptBackendProvider(p, center){
    const initials=p.isOrg
      ? (p.name.replace(/[^A-Za-z ]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'OR')
      : (p.name.split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'DR');
    const coords=(p.lat!=null&&p.lng!=null)?[p.lat,p.lng]:null;
    return {
      npi:p.npi, name:p.name, initials, isOrg:p.isOrg, specialty:p.specialty,
      taxonomies:p.taxonomies||[], address1:p.address1||'', city:p.city||'', stateAb:p.stateAb||'',
      postalCode:p.postalCode||'', fullAddress:p.fullAddress||'', mailingAddress:p.mailingAddress||'',
      phone:formatPhone(p.phone||''), phoneRaw:(p.phone||'').replace(/\D/g,''), fax:formatPhone(p.fax||''),
      gender:p.gender||'', soleProprietor:p.soleProprietor||'', credential:p.credential||'',
      status:p.status||'', enumerationDate:p.enumerationDate||'', lastUpdated:p.lastUpdated||'',
      insurance:p.insurance||{},
      lat:coords?coords[0]:null, lng:coords?coords[1]:null, geocoded:!!coords,
      distance:(coords&&center)?haversine(center,coords):null,
      color:PALETTE[hashStr(p.specialty||p.npi)%PALETTE.length],
    };
  }

  // Map one insurance answer to a display status, honoring the payer/plan
  // distinction (A2). A *payer-level* verified hit means the provider is listed in
  // that payer's network directory — NOT that a specific plan is accepted — so it is
  // deliberately never labeled "Confirmed". Only a plan-level program (e.g. Medicare)
  // earns "Confirmed". Estimated answers are always "Likely", never confirmed.
  // `filterable` (the plan's discriminating flag, A4) distinguishes a regional
  // estimate that can actually narrow results from a national "operates everywhere"
  // estimate that can't — the latter is honestly labeled area context, not a match.
  // Returns {cls, text} or null when there's nothing to show.
  function coverageStatus(info, filterable){
    if(!info) return null;
    const v=info.value, level=info.level||'payer';
    if(info.confidence==='verified'){
      if(v===true) return level==='plan'
        ? {cls:'yes', text:'Confirmed'}
        : {cls:'innet', text:'In-network'};
      if(v===false) return level==='plan'
        ? {cls:'no', text:'Not enrolled'}
        : {cls:'no', text:'Not listed'};
      return {cls:'unknown', text:'Unverified'};
    }
    if(v===true) return filterable===false
      ? {cls:'likely', text:'Operates in your area'}
      : {cls:'likely', text:'Likely · confirm'};
    return {cls:'unknown', text:'Unverified'};
  }

  // Format an epoch-seconds timestamp as a short, stable date for the "checked
  // <date>" provenance link. UTC so it doesn't drift across timezones in tests.
  function fmtDate(epochSeconds){
    if(epochSeconds==null) return '';
    const d=new Date(epochSeconds*1000);
    if(isNaN(d.getTime())) return '';
    return d.toLocaleDateString('en-US',{timeZone:'UTC',year:'numeric',month:'short',day:'numeric'});
  }

  return { TAXONOMY_MAP, PALETTE, toTitleCase, formatPhone, hashStr, haversine,
           cssEsc, esc, buildNpiParams, buildProviders, adaptBackendProvider,
           coverageStatus, fmtDate };
});
