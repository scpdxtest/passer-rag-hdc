import { Menubar } from 'primereact/menubar';
import "primereact/resources/primereact.min.css";
import "primeicons/primeicons.css";
import { useState, useEffect } from 'react';
import { blueFontSmall } from './mylib';
import { checkBCEndpoint, checkIPFSEndpoint } from './BCEndpoints.js';
import aboutIcon from './about.png';
import './Nav.css';
import loginIcon from './Login_37128.png';

const Navigation = () => {
    const [selectedModel] = useState(localStorage.getItem("selectedLLMModel") || 'Default -> mixtral');
    const [selectedOllama] = useState(localStorage.getItem("selectedOllama") || 'http://');
    const [ChromaDBPath] = useState(localStorage.getItem("selectedChromaDB") || 'http://');

    useEffect(() => {
        checkBCEndpoint().then(async (res) => {
            localStorage.setItem("bcEndpoint", res);
        });
        checkIPFSEndpoint().then(async (res) => {
            localStorage.setItem("ipfsEndpoint_host", res.host);
            localStorage.setItem("ipfsEndpoint_port", res.port);
        });
    }, []);

    const navlist = [
      { label: 'About', icon: <img src={aboutIcon} alt="About" width="22" height="22" />, command: () => {
          window.location.href = './#/about';
        }
      },
      {
        label: 'Create Vectorstore',
        icon: <span style={{ color: 'red' }} className="pi pi-fw pi-plus"></span>,
        items: [
          { label: 'From Paper Corpus', icon: <span style={{ color: 'red' }} className="pi pi-fw pi-book"></span>, command: () => { window.location.href = './#/dbfromcorpuspapers'; } }
        ]
      },
      {
        label: 'Paper Scoring',
        icon: <span style={{ color: '#7b1fa2' }} className="pi pi-fw pi-star"></span>,
        command: () => { window.location.href = './#/scorepapersbat'; }
      },
      {
        label: 'Chat',
        icon: <span style={{ color: 'green' }} className="pi pi-fw pi-comments"></span>,
        items: [
          { label: 'NewRAG (paper corpus chat)', icon: <span style={{ color: 'green' }} className="pi pi-fw pi-book"></span>, command: () => { window.location.href = './#/chatnewrag'; } }
        ]
      },
      {
        label: 'Configuration',
        icon: <span style={{ color: 'blue' }} className="pi pi-fw pi-cog"></span>,
        items: [
          { label: 'Settings', icon: <span style={{ color: 'blue' }} className="pi pi-fw pi-cog"></span>, command: () => { window.location.href = './#/selectmodel'; } },
          { label: 'Add/Remove Model', icon: <span style={{ color: 'blue' }} className="pi pi-fw pi-plus-circle"></span>, command: () => { window.location.href = './#/addmodel'; } }
        ]
      },
      { label: 'ManageDB', icon: <span style={{ color: 'purple' }} className="pi pi-fw pi-server"></span>, command: () => { window.location.href = './#/managedb'; } },
      { label: 'AnchorLogin', icon: <img src={loginIcon} alt="AnchorLogin" width="20" height="20" />, command: () => { window.location.href = './#/testwharf'; } }
    ];

    return(
        <header>
            <nav>
                <ul>
                    <Menubar
                        model={navlist}
                        end={
                            <div>
                                <div style={blueFontSmall}><b>OllamaAPI:</b>{selectedOllama} | <b>Model:</b>{selectedModel}</div>
                                <div style={blueFontSmall}><b>ChromaAPI:</b>{ChromaDBPath} | <b>BCName:</b>{localStorage.getItem('wharf_user_name')}</div>
                            </div>
                        }
                    />
                </ul>
            </nav>
         </header>
    )
}

export default Navigation;
