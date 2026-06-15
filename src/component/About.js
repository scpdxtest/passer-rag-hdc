import React, { useEffect, useState } from "react";
import backgroundImage from './PasserLogo4_GPT.png';
import { ContractKit } from "@wharfkit/contract";
import { APIClient } from '@wharfkit/antelope';
import packageJson from '../../package.json'; // Import version info from package.json

const About = () => {
    const [version, setVersion] = useState('');
    const [ramMarket, setRamMarket] = useState([]);

    const hello = 'Welcome to PaSSER: Platform for Retrieval-Augmented Generation';
    const aboutContent = [
        'PaSSER is a web application designed for implementing and testing Retrieval-Augmented Generation (RAG) models. It offers a user-friendly interface for adaptive testing across various scenarios, integrating large language models (LLMs) like Mistral:7b, Llama2:7b, and Orca2:7b.',
        'PaSSER provides a comprehensive set of standard Natural Language Processing (NLP) metrics, facilitating thorough evaluation of model performance.',
        'The platform fosters collaboration and transparency within the research community, empowering users to contribute to the advancement of language model research and development.',
        'This work was supported by the Bulgarian Ministry of Education and Science under the National Research Program “Smart crop production” approved by the Ministry Council No. 866/26.11.2020.'
    ];

    // const contractKit = new ContractKit({
    //     client: new APIClient({ url: localStorage.getItem("bcEndpoint") }),
    // });

    // const getRamRegister = async () => {
    //     try {
    //         const contract = contractKit.load("resourcestat");
    //         const cursor = (await contract).table("ramregister").query();
    //         const ramData = await cursor.all();
    //         setRamMarket(ramData);
    //         console.log(ramData);
    //     } catch (error) {
    //         console.error("Failed to fetch RAM register:", error);
    //     }
    // };

    useEffect(() => {
        // getRamRegister();
        setVersion(packageJson.version); // Set version from package.json
    }, []);

    return (
        <div style={{ display: 'flex', height: '100vh', width: '100%', fontFamily: 'Arial, sans-serif' }}>
            {/* Left Section with Background Image */}
            <div
                style={{
                    flex: '50%',
                    backgroundImage: `url(${backgroundImage})`,
                    backgroundSize: 'cover',
                    backgroundPosition: 'center center',
                    filter: 'brightness(0.9)',
                }}
            ></div>

            {/* Right Section with Content */}
            <div style={{ flex: '50%', backgroundColor: '#f9f9f9', color: '#333', padding: '40px', overflowY: 'auto' }}>
                <div style={{ marginBottom: '20px' }}>
                    <h1 style={{ fontSize: '2.5em', color: '#333', marginBottom: '10px' }}>{hello}</h1>
                    <p style={{ fontSize: '1em', color: '#888' }}>Version: <strong>{version}</strong></p>
                </div>

                {aboutContent.map((text, index) => (
                    <div key={index} style={{ marginBottom: '20px' }}>
                        <h2 style={{ fontSize: '1.2em', fontWeight: 'normal', color: '#555', lineHeight: '1.6' }}>
                            {text}
                        </h2>
                    </div>
                ))}

                {/* RAM Market Data */}
                {/* {ramMarket.length > 0 && (
                    <div style={{ marginTop: '30px' }}>
                        <h3 style={{ fontSize: '1.2em', color: '#333', marginBottom: '10px' }}>RAM Market Data:</h3>
                        <ul style={{ paddingLeft: '20px', color: '#555' }}>
                            {ramMarket.map((item, index) => (
                                <li key={index} style={{ marginBottom: '8px' }}>
                                    {JSON.stringify(item)}
                                </li>
                            ))}
                        </ul>
                    </div>
                )} */}
            </div>
        </div>
    );
};

export default About;