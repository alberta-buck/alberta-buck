// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract NoteBindingGroth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 17269339007125420435017385076298032648558905116036800167317165580267863063327;
    uint256 constant alphay  = 8298769059208403406707536659738406107699218080610564322476779891334978218882;
    uint256 constant betax1  = 19287679028377239150943411209093196901533308024455742180196324837626175744698;
    uint256 constant betax2  = 15446589398518190454274420272385151265977637145422343165550062847443974749946;
    uint256 constant betay1  = 5858245119689183400807064004799128131011760842567486047011829007784080776273;
    uint256 constant betay2  = 11545678145891239261012950117434374641078746363998546951851739104882335488744;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 14212683822834355807391123801207359269656731908440849875883109707536753844986;
    uint256 constant deltax2 = 7936510794367648148492342491914899979775052673911971857671000828784144985972;
    uint256 constant deltay1 = 3614583440986067507289604808060907823281290546794535110523813913759308715542;
    uint256 constant deltay2 = 338176358484926992409698697718003605363295689562073619259924020190925920118;

    
    uint256 constant IC0x = 19183985138455169152773285376849632981472157764558819742499705386108805284462;
    uint256 constant IC0y = 20590498208752342014252013137223777133609323380995494715187372737978245050905;
    
    uint256 constant IC1x = 8946198379192044544214598093979913642581063829240323806923200720371878338086;
    uint256 constant IC1y = 16365263216838722683428596692797768659101534949911169019913145182972881608735;
    
    uint256 constant IC2x = 10319962541707316915686811448430991623035111008132125499284704681249670704472;
    uint256 constant IC2y = 13453361206588824989245246394134492347924358455811116152740347424740932812008;
    
    uint256 constant IC3x = 8044517980851035518632173046020591306167081721047198692679696962508060918893;
    uint256 constant IC3y = 13021303377462852591571217166750257333652132293301128602051464902181595081782;
    
    uint256 constant IC4x = 21323150506858526345266252571445545782063853233311649841715009023092036720183;
    uint256 constant IC4y = 10444955600499204057870330305965635274036915584136687607628178254484619219642;
    
    uint256 constant IC5x = 14724442884813712253869613882384294575021218692993076836426274265733926884787;
    uint256 constant IC5y = 17757990397595320667145073330036026976302261533682109554199178031325648939443;
    
    uint256 constant IC6x = 7026720708663204211143095237964249959967740228907959304616684374478494118520;
    uint256 constant IC6y = 9315967144671739783472628480328265287075874466426010987568134804626639868745;
    
    uint256 constant IC7x = 16037174768990131824767441172420333177722828223113202095795706290988586903334;
    uint256 constant IC7y = 7560413307881596990773838139666256643161309835419451949971501530209563347964;
    
    uint256 constant IC8x = 18165475249239091412636084542881292843949366125599283131593154339757735415792;
    uint256 constant IC8y = 21780047439005108394462090015718476021333339258552892618739323183459559832594;
    
    uint256 constant IC9x = 14869103215400746854172000820794353760419523710382180165573857107393891338243;
    uint256 constant IC9y = 6301615323295875255270790297158828311115031953864680338435198716828564676394;
    
    uint256 constant IC10x = 2418123705706350211413436517185448880288848605570184590595545843452547372103;
    uint256 constant IC10y = 509936948551730642402976397193206183700071349437804069807198020905594046315;
    
    uint256 constant IC11x = 19262909692035585077593756744677770422262621458226525098900242257305218650496;
    uint256 constant IC11y = 599651025409044969835069341219536956367921706342288443585919763625655252868;
    
    uint256 constant IC12x = 20827621964698078368410092795774221991477260830287474781995147412544647399035;
    uint256 constant IC12y = 18773525536112081055697330973396088966967320088421259400951350734984632608517;
    
    uint256 constant IC13x = 11141548568536117796557392143404304692199540389475694326625948041537555949776;
    uint256 constant IC13y = 8574096623772439749072786720269401719752696251781877866671439935816059024009;
    
    uint256 constant IC14x = 2901641083815398353065150846907690892052248650954447727568015843661555585647;
    uint256 constant IC14y = 12977479376470228916661064972013796203830080083799069529365365602428336480522;
    
    uint256 constant IC15x = 7844759927537344745770659000830994493738380897665687852292167871983515435310;
    uint256 constant IC15y = 2765137315959464990630341576709311906512211279498020630863412141066737079098;
    
    uint256 constant IC16x = 6752073065425589852658896801791183267344474476912248364295467060443010030404;
    uint256 constant IC16y = 16613597767678564420982650786273020719692350688679614734545219192794258065729;
    
    uint256 constant IC17x = 20358981054783637400959315265913074729851641633992105900016860398322605166166;
    uint256 constant IC17y = 5892338857578741249593580610695564671231648789585004377140919129692137652039;
    
    uint256 constant IC18x = 5143243714863969611102146984085486672630615039735763777226435099133388720941;
    uint256 constant IC18y = 6458141768857820298322143015810108371433235401095099018097921101437407608081;
    
    uint256 constant IC19x = 7428104841159765867698634281757697084151731770127115779995021773221416398742;
    uint256 constant IC19y = 16614017303068424132021025983898137983038326388237736172126108492117721432150;
    
    uint256 constant IC20x = 10872604857612081910077402671845975243720964109620729239424620541276150094829;
    uint256 constant IC20y = 12141422574849739574953686612077687514616110849846775379781466933892441711354;
    
    uint256 constant IC21x = 4734315833639101528425343801281167290248070536609562011439035966300430760615;
    uint256 constant IC21y = 6785814961136713131483591168372496868960431272024010245797416968128553600262;
    
    uint256 constant IC22x = 1774803746506856644575808667399665220030667910182241206990440400827909334965;
    uint256 constant IC22y = 21101006981096154332503156066734106027081599043939564216522877598205729712654;
    
    uint256 constant IC23x = 1519334376225928106042688442880063569441647793808191584206001193353761058820;
    uint256 constant IC23y = 17893795754060097276689708960396812977201103532773972553693452265332332314355;
    
    uint256 constant IC24x = 18392640069233645690488961175933793596291322922781949986826562165302289482772;
    uint256 constant IC24y = 491497730197780723825537588455689385684985002198858839490573219090568725288;
    
    uint256 constant IC25x = 9471884908521637459409212998804132420207685527252620413962781733008042370093;
    uint256 constant IC25y = 7858429757729126033760228898625000444939439080796868697158490653560839062678;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[25] calldata _pubSignals) public returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 96), calldataload(pB))
                mstore(add(_pPairing, 128), calldataload(add(pB, 96)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 64)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
