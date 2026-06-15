import { useState, useEffect } from "react";
import axios from "axios";
import { Dropdown } from "primereact/dropdown";
import './SelectModel.css';
import { InputText } from "primereact/inputtext";
import configuration from './configuration.json';
import { SelectButton } from 'primereact/selectbutton';

const SelectModel = () => {
    const [availableModels, setAvailableModels] = useState([]);
    const [selectedModel, setSelectedModel] = useState(null);
    const [ChromaDBPath, setChromaDBPath] = useState(null);
    const [availableCromas, setAvailableCromas] = useState(configuration.passer.Chroma.map(item => item.url));
    const [selectedOllama, setSelectedOllama] = useState(null);
    const [selectedMultiAgent, setSelectedMultiAgent] = useState(null);
    const [temperature, setTemperature] = useState(0.2);
    const retOptions = ['Normal', 'Score'];
    const [retriever, setRetriever] = useState();
    
    const availableOllamas = configuration.passer.Ollama.map(item => item.url);
    const availableMultiAgents = configuration.passer.MultiAgent.map(item => ({
        label: item.name,
        value: item.url
    }));
    
    const [errorMessage1, setErrorMessage1] = useState('');
    const [errorMessage2, setErrorMessage2] = useState('');
    const [errorMessage3, setErrorMessage3] = useState('');
    const [symScore, setSymScore] = useState(0);
    const [k, setK] = useState(0);
    const [kInc, setKInc] = useState(0);

    useEffect(() => {
        // Load all values from localStorage
        setSymScore(Number(localStorage.getItem("symScore")) || 0.9);
        setK(Number(localStorage.getItem("k")) || 100);
        setKInc(Number(localStorage.getItem("kInc")) || 2);
        setRetriever(localStorage.getItem("retriever"));
        
        // Load MultiAgent API selection
        const savedMultiAgent = localStorage.getItem("selectedMultiAgent") || 'http://127.0.0.1:8004';
        setSelectedMultiAgent(savedMultiAgent);
        
        // Load Temperature
        const temp = localStorage.getItem("chatTempreture") || '0.2';
        setTemperature(parseFloat(temp));
        
        // Load Ollama selection - FIXED
        const savedOllama = localStorage.getItem("selectedOllama") || 'http://127.0.0.1:11434';
        setSelectedOllama(savedOllama);
        
        // Load ChromaDB selection - FIXED
        const savedChroma = localStorage.getItem("selectedChromaDB");
        if (savedChroma) {
            setChromaDBPath(savedChroma);
        }
        
        // Fetch available models from selected Ollama - FIXED
        axios.get(savedOllama + '/api/tags')
        .then((res) => {
            setAvailableModels(res.data);
            
            // Load selected model from localStorage - FIXED
            const savedModelName = localStorage.getItem("selectedLLMModel");
            if (savedModelName && res.data.models) {
                const foundModel = res.data.models.find(m => m.name === savedModelName);
                if (foundModel) {
                    setSelectedModel(foundModel);
                }
            }
        })
        .catch((err) => {
            console.log(err);
            console.error("Ollama is not available at", savedOllama);   
        });
    }, []);

    return (
        <div>
            <h1>Configurations</h1>
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', flexWrap: 'wrap', gap: '20px' }}>
                <div>
                    <h3>Select Ollama Path</h3>
                    <Dropdown
                        value={selectedOllama} 
                        options={availableOllamas} 
                        onChange={(e) => {
                            setSelectedOllama(e.value); 
                            localStorage.setItem("selectedOllama", e.value); 
                            window.location.reload(false);
                        }} 
                        placeholder="Select an Ollama" 
                    />
                </div>
                
                <div>
                    <h3>Select MultiAgent API</h3>
                    <Dropdown
                        value={selectedMultiAgent}
                        options={availableMultiAgents}
                        onChange={(e) => {
                            setSelectedMultiAgent(e.value);
                            localStorage.setItem("selectedMultiAgent", e.value);
                            console.log("MultiAgent API set to:", e.value);
                            window.location.reload(false);
                        }}
                        placeholder="Select MultiAgent API"
                        style={{ minWidth: '200px' }}
                    />
                </div>
                
                <div>
                    <h3>Select Model</h3>
                    <Dropdown
                        value={selectedModel} 
                        options={availableModels.models} 
                        onChange={(e) => {
                            setSelectedModel(e.value); 
                            localStorage.setItem("selectedLLMModel", e.value.name); 
                            window.location.reload(false);
                        }} 
                        placeholder="Select a Model" 
                        optionLabel="name"
                    />
                </div>
                
                <div>
                    <h3>Select ChromaDB</h3>
                    <Dropdown
                        value={ChromaDBPath} 
                        options={availableCromas} 
                        onChange={(e) => {
                            setChromaDBPath(e.value); 
                            localStorage.setItem("selectedChromaDB", e.value); 
                            window.location.reload(false);
                        }} 
                        placeholder="Select a ChromaDB" 
                    />
                </div>
                
                <div>
                    <h3>Chat Temperature</h3>
                    <InputText 
                        style={{width: '100px'}}
                        value={temperature} 
                        onKeyPress={(e) => {
                            if (e.key === 'Enter') {
                                localStorage.setItem("chatTempreture", parseFloat(e.target.value) || '0.2');
                                window.location.reload(false);
                            }
                        }}                    
                        onChange={(e) => {setTemperature(e.target.value)}} 
                        placeholder="Chat Temperature" 
                    />
                </div>
                
                <div>
                    <h3>Retriever</h3>
                    <div className="card flex justify-content-center">
                        <SelectButton 
                            value={retriever} 
                            onChange={(e) => {
                                setRetriever(e.value); 
                                localStorage.setItem("retriever", e.value);
                                console.log('retriever', localStorage.getItem("retriever"));
                            }} 
                            options={retOptions} 
                        />
                    </div>
                    {retriever === 'Score' ? (
                        <div style={{ width: '100%', border: '1px solid black', marginTop: '10px', padding: '10px' }}>
                            <div className="p-field p-grid">
                                <label htmlFor="input1" className="p-col-fixed" style={{width:'120px'}}>Similarity score</label>
                                <div className="p-col">
                                    <InputText 
                                        id="input1" 
                                        type="number" 
                                        min="0" 
                                        max="1" 
                                        step="0.01" 
                                        placeholder="Similarity score" 
                                        value={symScore}
                                        onChange={(e) => {
                                            const value = e.target.value;
                                            setSymScore(value);
                                            localStorage.setItem("symScore", value.toString());
                                        }}
                                        onBlur={(e) => {
                                            const value = e.target.value;
                                            if (value === '.' || (parseFloat(value) >= 0 && parseFloat(value) <= 1 && (parseFloat(value) * 10000) % 1 === 0)) {
                                                setErrorMessage1('');
                                            } else {
                                                setErrorMessage1('Similarity score must be between 0 and 1 and have a maximum of 4 decimal places');
                                            }
                                        }}
                                    />
                                </div>
                            </div>
                            <div className="p-field p-grid">
                                <label htmlFor="input2" className="p-col-fixed" style={{width:'120px'}}>k</label>
                                <div className="p-col">
                                    <InputText 
                                        id="input2" 
                                        type="number" 
                                        step="1" 
                                        placeholder="k" 
                                        value={k}
                                        onChange={(e) => {
                                            const value = parseFloat(e.target.value);
                                            setK(value);
                                            localStorage.setItem("k", value.toString());
                                        }}
                                        onBlur={(e) => {
                                            const value = parseFloat(e.target.value);
                                            if (value <= 0 || value % 1 !== 0) {
                                                setErrorMessage2('k must be greater than 0 and without decimals');
                                            } else {
                                                setErrorMessage2('');
                                            }
                                        }}
                                    />
                                </div>
                            </div>
                            <div className="p-field p-grid">
                                <label htmlFor="input3" className="p-col-fixed" style={{width:'120px'}}>k increment</label>
                                <div className="p-col">
                                    <InputText 
                                        id="input3" 
                                        type="number" 
                                        step="1" 
                                        placeholder="k increment" 
                                        value={kInc}
                                        onChange={(e) => {
                                            const value = parseFloat(e.target.value);
                                            setKInc(value);
                                            localStorage.setItem("kInc", value.toString());
                                        }}
                                        onBlur={(e) => {
                                            const value = parseFloat(e.target.value);
                                            if (value <= 0 || value > k || value % 1 !== 0) {
                                                setErrorMessage3('k increment must be greater than 0, less than k and without decimals');
                                            } else {
                                                setErrorMessage3('');
                                            }
                                        }}
                                    />
                                </div>
                            </div>                        
                            {errorMessage1 && <div style={{ color: 'red' }}>{errorMessage1}</div>}
                            {errorMessage2 && <div style={{ color: 'red' }}>{errorMessage2}</div>}
                            {errorMessage3 && <div style={{ color: 'red' }}>{errorMessage3}</div>}
                        </div>
                    ) : null}                
                </div>                
            </div>
            
            <div style={{ marginTop: '20px', padding: '15px', background: '#f0f0f0', borderRadius: '8px' }}>
                <h4>Current Configuration Summary</h4>
                <p><strong>Ollama:</strong> {selectedOllama || 'Not selected'}</p>
                <p><strong>MultiAgent API:</strong> {selectedMultiAgent || 'Not selected'}</p>
                <p><strong>ChromaDB:</strong> {ChromaDBPath || 'Not selected'}</p>
                <p><strong>Model:</strong> {selectedModel?.name || 'Not selected'}</p>
                <p><strong>Temperature:</strong> {temperature}</p>
            </div>
        </div>
    );
}

export default SelectModel;