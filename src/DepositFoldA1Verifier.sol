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

contract DepositFoldA1Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 20901312453931986983070371975751072566398443441960436590636518297529489694139;
    uint256 constant alphay  = 21697131480926335546036040991413011755153140230317664815989518148158724370212;
    uint256 constant betax1  = 4869487231785253308212429570614741443191402866210819877611793555879218415484;
    uint256 constant betax2  = 8494223465375928023871052138064538434365863294294498107508764578998413175191;
    uint256 constant betay1  = 13692213518456195651330939167802977105042737180923530430942149449680303886878;
    uint256 constant betay2  = 6329045905833941605730059747255744716806210507261219104759569890353192015645;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 12640041589823440111952235610969278236904233931352183244491278988272053230542;
    uint256 constant deltax2 = 18536484056953944513357725022206543479798562362935734436683234660581457542847;
    uint256 constant deltay1 = 3624554847128460345558442274385918355246971538544467303049266177449893657595;
    uint256 constant deltay2 = 18724415380032220795316188718499832015200858390197857038846121680376687331946;

    
    uint256 constant IC0x = 10312275606957288630989877549311081013249532791631588540316025335333334943753;
    uint256 constant IC0y = 2939960183794032827676275250444083024664822540198518801675034596171825386016;
    
    uint256 constant IC1x = 15238363453389825321436294298958387703043587063097404530451360619593854273528;
    uint256 constant IC1y = 2775964653799685132791464593673546513316146047534471718138653985110654559222;
    
    uint256 constant IC2x = 6359307194299287037382329573014140988705522523431933264586217484601566916192;
    uint256 constant IC2y = 18741023103832093563617589295161448849882611614595825802084226869546741035396;
    
    uint256 constant IC3x = 3349789083314803821531752317878495424907451698402475358195073599597129800047;
    uint256 constant IC3y = 12871406831922959714194247440567652282150591404153082178441391605955517404033;
    
    uint256 constant IC4x = 12341864213087286388559821226364737431701993647759675526560857320301544096343;
    uint256 constant IC4y = 5107193562444731904742579272782967993140838265728213383123641985820465496320;
    
    uint256 constant IC5x = 6049122733765193387893105566261148421297357794699770371472525181499188790652;
    uint256 constant IC5y = 19502532284652606972364615544359967693209168746463849662827753792907483256503;
    
    uint256 constant IC6x = 5760908614860735852840360238822361648448999840903001495065843042644871274560;
    uint256 constant IC6y = 1112877044790952179605532773590126013538825566676221547544609660208033052211;
    
    uint256 constant IC7x = 12435130631862754286810247492582385918562037477440688935560875455290229810863;
    uint256 constant IC7y = 17621934117504281594284062863818601163107628813897928009598408180379193975941;
    
    uint256 constant IC8x = 15784347456176465981785030596447644768527320120903289943381204186001922215177;
    uint256 constant IC8y = 5060797364410659473336558321158007953376615386541740017010636974388581158923;
    
    uint256 constant IC9x = 1078548708827956521961746183987069211809127218506981876377537679711868453089;
    uint256 constant IC9y = 3542684787387091909988867658033667456918043739174436634505109297771035696745;
    
    uint256 constant IC10x = 5355710384423432296306327729377067222417419626118508501252256395712255588969;
    uint256 constant IC10y = 15960228605701198385394778476556467040007773073720212698880691415512676204638;
    
    uint256 constant IC11x = 3108721200830678084383124296995813574393746215647691031415031862350633389336;
    uint256 constant IC11y = 3023218058688084229844192099696173218159704911218940942486804191297333946206;
    
    uint256 constant IC12x = 19537199963007653796500432486035367450883371912844397191564366940357722940946;
    uint256 constant IC12y = 1767489350430427679852255630454483042943473944344223844264942996241811185791;
    
    uint256 constant IC13x = 14691401170736626693556380862651339789589201126528782123324004625958282255767;
    uint256 constant IC13y = 16596111818146328392673874116042134169811941376851594057194478179727638518104;
    
    uint256 constant IC14x = 4399727822602494377857180511935118713403303027650872789908673253525954108843;
    uint256 constant IC14y = 20949285765034381270000033720647678413244962471055150745834530375082798804778;
    
    uint256 constant IC15x = 15461445277237053612233437322516006092108238615178570435275222438567378564507;
    uint256 constant IC15y = 9593963801299490216825269803014815877606482586739702381090425714563370279110;
    
    uint256 constant IC16x = 11443036569981592485540311323964849823653166187192815131146640747745632512578;
    uint256 constant IC16y = 800108221484461043626085358632417797122313518410895753947211852408893420356;
    
    uint256 constant IC17x = 15555726280981394695821900633701637694225798588874852219117984596610215792100;
    uint256 constant IC17y = 14097308800116890411679093784540102724945634214330744589627824335718316469176;
    
    uint256 constant IC18x = 4825021158749534952155040467312985729539458587066967435908866998034618464012;
    uint256 constant IC18y = 19469603315017960481804966884761317756981634117769936098249763900125470126252;
    
    uint256 constant IC19x = 19578022702533407701171743767010270576152334036017959007762013354710403787057;
    uint256 constant IC19y = 7311594124196937617348890251942591204146124431700841900603706150334093608681;
    
    uint256 constant IC20x = 15344846676935271359030226653306247292748014328779753402047287084776117178737;
    uint256 constant IC20y = 4136494477645300092860505310125951296516914559027320273299841361187518213290;
    
    uint256 constant IC21x = 19640490384191861456684257573342146126709669528839676819471678083898499347343;
    uint256 constant IC21y = 1880873548560479359869855469211277002555114819425265585805758409365143436505;
    
    uint256 constant IC22x = 13873326999972374760544296170822209648113884256255281608934279019317664957280;
    uint256 constant IC22y = 16409798493374416258028910175468279985867453390320995390910343675453739891200;
    
    uint256 constant IC23x = 11652386377927601927920830118459384711211488699442952290223230670256689221788;
    uint256 constant IC23y = 1933958690051601699048094136463662247805008199908422878567048307314806825230;
    
    uint256 constant IC24x = 9580379636704532375046016902174248006106497279894951391802765370100219322886;
    uint256 constant IC24y = 1300842410692218659576028287544716509928308856419042380808574500623102856090;
    
    uint256 constant IC25x = 21262221677049275191281493415191178169010512211738169453737304266250474557056;
    uint256 constant IC25y = 16025284716958348900126816511503635335980776323473729575487220601249971996736;
    
    uint256 constant IC26x = 9513516139270778945426200547066245036290437438458859046982481513360240745872;
    uint256 constant IC26y = 4214113088565119905703514307306833900521438958090719657972887922530406818184;
    
    uint256 constant IC27x = 12101687586668860241079986479240338761562253567010658231039804680493270227610;
    uint256 constant IC27y = 11854037383581218994637672860315270872494712889721222171751575134220646100428;
    
    uint256 constant IC28x = 12986953352543984883212927161817497319356618533674899551511373592379079944940;
    uint256 constant IC28y = 1410620868070045615826956053469558738415179384286747915260149083726437671284;
    
    uint256 constant IC29x = 10798723324799139913515947386874311143415999055420621489649546678260366676781;
    uint256 constant IC29y = 567702670013014147130991553208023279415603284831800018399515919726122672659;
    
    uint256 constant IC30x = 12326804524346885823133926427876128586915845869508350347166297777154138339108;
    uint256 constant IC30y = 21671547057457361669349423718135540491108332476459258627036343129538565523594;
    
    uint256 constant IC31x = 14350476550595233012782216787738942916080631264046505370830493093686818296739;
    uint256 constant IC31y = 19825541813774386501813080536755904917680935404912501903150710189190596060914;
    
    uint256 constant IC32x = 13426189495180712758224073579728001737818944226979089448922549246936935959368;
    uint256 constant IC32y = 12677887537708200945209677939795880255762083420500564361250762077844943953073;
    
    uint256 constant IC33x = 8631527505621327602738706231694995326153040373319736153810033828645323604487;
    uint256 constant IC33y = 5230481190658942437819011210156879369295829850822204754278359537282013523608;
    
    uint256 constant IC34x = 14773506459424714918521653248409263069261570880499622269631032389075556605683;
    uint256 constant IC34y = 7907596836462047389042658570509471965369453178745941359554995912908973631315;
    
    uint256 constant IC35x = 268363259864135481845958055790683161159137643471428805371269809534327399657;
    uint256 constant IC35y = 20312923654061867059461138311157946745985248883567900782901079662778248853534;
    
    uint256 constant IC36x = 19025626292560765376040044104095150076865991450629659787464312325951797807853;
    uint256 constant IC36y = 8222855799822995751206397730360574842715811690583465152983518479674207056286;
    
    uint256 constant IC37x = 18610363457072229845648910585086650350799274164092234348982607715292142025159;
    uint256 constant IC37y = 16351780610380107215231803567988498256919338799397841923267629470838641621669;
    
    uint256 constant IC38x = 4980803615999040534037788955244045981599012999204649130217826575177390762844;
    uint256 constant IC38y = 19706522718138267645793822902256589989209839907233095687872126401178542498202;
    
    uint256 constant IC39x = 5119105290154560714080442562338902078375206991439268326941485791614445199133;
    uint256 constant IC39y = 14830274051361670008320022880136850826063420394824427715412584455106173916482;
    
    uint256 constant IC40x = 11812745524135505201604964230778567442090054226021256406200682726620873889405;
    uint256 constant IC40y = 6294933619629456672084418156646436054135918832519143539010926845615140346542;
    
    uint256 constant IC41x = 11060339925574463714906953013967904650177274141414861561347322630783196242816;
    uint256 constant IC41y = 6747300604494739329010678874704661137309732833818990755121533256487262594930;
    
    uint256 constant IC42x = 16465124377495828301658227083581345771036521447018898027469653211712188950028;
    uint256 constant IC42y = 18424103673756537949986087213633776637884638775150880542100650736073211103973;
    
    uint256 constant IC43x = 21688189792326869979138678316560689599824462537888741376516509758608027378697;
    uint256 constant IC43y = 6146613006091763258383212946908973793148604136899834232885152936670821400756;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[43] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                
                g1_mulAccC(_pVk, IC43x, IC43y, calldataload(add(pubSignals, 1344)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

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
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            
            checkField(calldataload(add(_pubSignals, 1344)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
