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

contract MintBatchA2N4Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 3631344086447600850896190419206420896657050492374191812883457347673571379922;
    uint256 constant alphay  = 14989005213188149616231819742625774045352845752597498751888940135169747957878;
    uint256 constant betax1  = 9288001167759986798601287554112931403268890387295497334378334514907605789422;
    uint256 constant betax2  = 18225411497800851856763395424903998811621518034192521220589788367768147826470;
    uint256 constant betay1  = 4912977382675421359068396990903804674786508328236952129039925973168636116033;
    uint256 constant betay2  = 10553400317604699926820584447706587103271842941642344264770658229076039827844;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 19654671319889263892844894482561121333684572659990212746408175277966681151602;
    uint256 constant deltax2 = 5278034860267340354052805145774673091029320518893015807160068651671852236521;
    uint256 constant deltay1 = 1445119739715376755118939324769671138136769654207244826995777955558218119139;
    uint256 constant deltay2 = 5554649794880518674191671615824938755170445402707907647927173120719123050879;

    
    uint256 constant IC0x = 9717086666868112185803934226021447391089403203045988040403558184466580709794;
    uint256 constant IC0y = 17905633312180733045028501952566721681325001137696848672566954758334433318996;
    
    uint256 constant IC1x = 4545671121192609173510011202609296030900211607643427152286387946440931821944;
    uint256 constant IC1y = 10888877516694244189615096053474979364688679108969787884203559187091667884541;
    
    uint256 constant IC2x = 11842170964631860152508864757806041384434423553407647772708744057286426934565;
    uint256 constant IC2y = 12620765503929327409175485318527560490684089909395583168564821980295143835508;
    
    uint256 constant IC3x = 1687627430497155619559750959511497378916964946658749892126985958636603020431;
    uint256 constant IC3y = 7186881803216394297045244860808272015120488361025000199943954712000383562019;
    
    uint256 constant IC4x = 7823144134813544808016715448013592242650965375941179088967263737687513264997;
    uint256 constant IC4y = 14200828149746338998168078431410667624062317470209301156164910593656939605678;
    
    uint256 constant IC5x = 9712941034848394773887240763646923113801682993358695665782740330511329851239;
    uint256 constant IC5y = 1920549436228029090327775694453166622142370592055541285267581913742217888427;
    
    uint256 constant IC6x = 8161192917139367990629937178168044202577668498375920947437627316841393078109;
    uint256 constant IC6y = 15657868721197223300947848332856814737231324153480790601831735235686607945180;
    
    uint256 constant IC7x = 18471393171911854310603122516058434874864145631313425677231601529818388868777;
    uint256 constant IC7y = 12949103734734203372328003491425530600617910637244404112954111976107847455192;
    
    uint256 constant IC8x = 14724176828759092666213150781016827120481084577471805929833963828328988696313;
    uint256 constant IC8y = 6909623144978083058609300474699630862506843532544679515896428196671338224256;
    
    uint256 constant IC9x = 8839303013598212172771425858364299845295465191928682652219281952598983737723;
    uint256 constant IC9y = 21701320369939791330451993456057638055999820573282176723849257251381746760645;
    
    uint256 constant IC10x = 13538329448248333946777319103977930672964460918607234324958486689282171042524;
    uint256 constant IC10y = 4766940309100321357483258557934873674986933990480839016231495346588588044461;
    
    uint256 constant IC11x = 1699934244481886014187823930419206872434309393969027277122251726103521451239;
    uint256 constant IC11y = 8115511374856249573304969515029054892624551274687453800596746077391176663048;
    
    uint256 constant IC12x = 17828861400120367137651602855333161929586428553632303055844295840406633663452;
    uint256 constant IC12y = 7145131218444215797240109674007049136035564141112209729699882859496869726080;
    
    uint256 constant IC13x = 16217200999031912181308542351266743043859483817354080777511339477735533192464;
    uint256 constant IC13y = 19522934782918816440147275986892262031401967586316937582821477000815324846737;
    
    uint256 constant IC14x = 16646258584893229145141229270074692684683622879671225484001542999148952111319;
    uint256 constant IC14y = 1549733051644247881101870234103032770265558897731080756249873393003841270315;
    
    uint256 constant IC15x = 1112143246493790234156701343000607936105928359460282035388803373231891013819;
    uint256 constant IC15y = 2864876791258786293835959389298891571797905206669026879337894867357052379184;
    
    uint256 constant IC16x = 21206558731786448405989694168061669756928810284160873878290766270942785351256;
    uint256 constant IC16y = 21691760173211418897169433162739062862452955694478911058474197437580995363640;
    
    uint256 constant IC17x = 3668343243971095571814439607950312129560947922355310756687524509199122077963;
    uint256 constant IC17y = 19469040602141595619847551077569306197499690954883969559268847527588486531769;
    
    uint256 constant IC18x = 6015005721259571580033421524239960211420589850246946221673195680521519559040;
    uint256 constant IC18y = 15331081998804047267420582429137941555642619491819164125103693928651680476946;
    
    uint256 constant IC19x = 17032207150219031899912142544588523113710936729773656092866378389345429994960;
    uint256 constant IC19y = 2968817769438146906218254053531834981705433696171803760527827350840120088209;
    
    uint256 constant IC20x = 9213921836691069591076378083558139726688207926586465117088824918523555458182;
    uint256 constant IC20y = 13198217785164320593458782986321684809894028430388260400038247427971166235959;
    
    uint256 constant IC21x = 17939436653338040536036925414647731833516577068574666655178751244395463800740;
    uint256 constant IC21y = 2070969282789855250767672575828159612851939149878744345857256874918167648662;
    
    uint256 constant IC22x = 18801727887805672376652172932149365991983457540984273665671897641779859146224;
    uint256 constant IC22y = 11882561877806450683180274219963760191067365271785316130274163821135574631802;
    
    uint256 constant IC23x = 3378829721461919923230907773812084174718522805700231235706094061606531490703;
    uint256 constant IC23y = 1642015163909612558434410172687128270353705479053193065292818566510393045420;
    
    uint256 constant IC24x = 14776870769371670168552672979928242533162913632393792305048001902769551116511;
    uint256 constant IC24y = 6250113869128768345947911501057668706941552558389645194550600385213218131746;
    
    uint256 constant IC25x = 20525108142339563459725074008930080458249241152630832251175898286519012637829;
    uint256 constant IC25y = 6876793436205035780154356167177482844331784018400042360467833701909103624685;
    
    uint256 constant IC26x = 9166436715279434607650796598725415904603542534488978884127032282597727965833;
    uint256 constant IC26y = 12062167955742972950296552548215544676015871742854675982787015010957196162175;
    
    uint256 constant IC27x = 20697629899159321741944904555798701105784206022442915548885344369101549003486;
    uint256 constant IC27y = 4247645810495048072039152159565441103224305439087431835293657551713673952231;
    
    uint256 constant IC28x = 14480090995809592157544980244469140570017971541295855363748529556770201737151;
    uint256 constant IC28y = 16374861788286011582858093930319399658549304157207115112088853250710671338677;
    
    uint256 constant IC29x = 223691494162331357718148987260811128185034768315822150924869355529932030844;
    uint256 constant IC29y = 4292692224790220990482242928627512965087569720418499434822144084520459908474;
    
    uint256 constant IC30x = 15755393633407261904831872190012214784649807898045210868067769368097488796980;
    uint256 constant IC30y = 21083980539725687485289230030183117356518008032847272995370850928760850258013;
    
    uint256 constant IC31x = 13155408446811970986403265216178623766847093701454015602253376816149078861244;
    uint256 constant IC31y = 14816222581229620516850063675913147449158881734591025577371253267145747499656;
    
    uint256 constant IC32x = 1054316287416829935557244979753741332197910914850709449831459598833649261003;
    uint256 constant IC32y = 969292647115593109593996504170628902398471519931973955679972083577250088995;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[32] calldata _pubSignals) public view returns (bool) {
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
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
