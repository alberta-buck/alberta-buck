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

contract MintBatchN16Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 7978374530620261980907563326553864501965289527526872291934547527166537747551;
    uint256 constant alphay  = 5901113449839617362854698006124082792885893177043162830865317277785118545420;
    uint256 constant betax1  = 18220824476844239577768066887046680990910624075694529208134813320434359817912;
    uint256 constant betax2  = 16450672241897021868604598054257760272991953374496313798435261611576668251269;
    uint256 constant betay1  = 11726201621433110351792550440521544935687171988284287970664993912055275635786;
    uint256 constant betay2  = 20979770059934782849226372365594771099262336599978723952245345619213052609192;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 17761674242434934918370917166172286021406339588022691477627078409539783317392;
    uint256 constant deltax2 = 12811877058027549213898412616926929029115893372513797348668553697046701114517;
    uint256 constant deltay1 = 8831297115792095659942236201994387825467803806713593442688014357997711796851;
    uint256 constant deltay2 = 16622919295142210570875827274888803073812407655243396243113682174382093769971;

    
    uint256 constant IC0x = 15943825795902132921439343117784962022964365459431328886884285599762479803006;
    uint256 constant IC0y = 2681064972575360836409248271272486844569844381592905660167599501116557765317;
    
    uint256 constant IC1x = 820986769158622250001904178473300873526930952158564202326748251298849667423;
    uint256 constant IC1y = 20788045029024952189336783194082722285318870262076436211138912174118153478520;
    
    uint256 constant IC2x = 12205073416985435646061399368505105770202921965241390130889583815573510352423;
    uint256 constant IC2y = 18598141994655781379214040410213423807736103610912171865678724678965902200799;
    
    uint256 constant IC3x = 644282143782760103660300628175673070741558342950466943269139163464676964717;
    uint256 constant IC3y = 3955732726665008874442462453019120109560456099996945746392117192442038228944;
    
    uint256 constant IC4x = 10854402851526044114256795372336127302744839000813140320200128440943334320093;
    uint256 constant IC4y = 19485724316295473462856225981685947661476558137508382350047747452520118792600;
    
    uint256 constant IC5x = 21685957175636936683297476807830433439749750220390544698037013832295301528863;
    uint256 constant IC5y = 12127666221250328573924374525738801472501960744590241858454579983258250981477;
    
    uint256 constant IC6x = 14206642478390478345086522284483771231495557102499451063255986881612912826854;
    uint256 constant IC6y = 6864747148670652681429219702499165896789639629706255708697427720600955892252;
    
    uint256 constant IC7x = 5528355047479151993211314725887565115656823378572968289383262445264934520838;
    uint256 constant IC7y = 5177295174549586815202373799972265922695370996895385283995956520673756204610;
    
    uint256 constant IC8x = 10315486029555680878366979006483929616002022863182773136629240681875874626632;
    uint256 constant IC8y = 15954824942392838851586043775045664893684786235628253311938969334330754752953;
    
    uint256 constant IC9x = 7656739021473522256943687450130525061048346616705314960360943546751624616115;
    uint256 constant IC9y = 4339563894904538085158734834845375833424910232024895153123735860824625854809;
    
    uint256 constant IC10x = 1029394493794905858820308354013751496934746967248534697908885055974280433803;
    uint256 constant IC10y = 3837087038162440277364275936241112953521433950830016000740846531884777110038;
    
    uint256 constant IC11x = 18163897663351903226739785952236523445241152556842294616490405201355589404669;
    uint256 constant IC11y = 17105781000655722308799710469046863907285059324399723779311945431296079686199;
    
    uint256 constant IC12x = 6400289336286340957446826855127308118118842570885938627054327812328065788382;
    uint256 constant IC12y = 16065310277398515418753846689745887701747085831342175948912571179193123025642;
    
    uint256 constant IC13x = 856162512597354719975133941756593478415989120057727115500712184201940704769;
    uint256 constant IC13y = 15999338513132405609298600410374403281701572894067660314821732128948886140313;
    
    uint256 constant IC14x = 4108648812296967406710051445896248034998042718089673622582880159744337373078;
    uint256 constant IC14y = 5400268581220267103494538134234073083939300878533349228328970179842437422431;
    
    uint256 constant IC15x = 2634339699578067745296495347592881349160235127583934453910063235139296182896;
    uint256 constant IC15y = 11082499140407042077046085208966839962826066286962062402559141584975061182099;
    
    uint256 constant IC16x = 8427908836888819443345549123576703277848332932439994966504275640549017936459;
    uint256 constant IC16y = 18538456007172290524501945580773026625779004451309529233624858095946340731789;
    
    uint256 constant IC17x = 4497536186139891327171016930158545309161710203701483431944685041189603517379;
    uint256 constant IC17y = 5118199945693681326121402988740319237960366378193972207135452831973547394736;
    
    uint256 constant IC18x = 5627372863910852169472961922231277735604504606324000152563822937263646408759;
    uint256 constant IC18y = 5266032290394018430085589739239691403259837112912002039731218496913372865722;
    
    uint256 constant IC19x = 14302441510015734988368108015382445936392659544148510595041427828694506623916;
    uint256 constant IC19y = 4808308449858466472751924483959233919144454647139516135980733724996803872119;
    
    uint256 constant IC20x = 2164318669517013468353193727246190753670271267092842885439854949479949347302;
    uint256 constant IC20y = 15759578288069133786407864346147033177316767685425601672101133347231269129170;
    
    uint256 constant IC21x = 14061236404875454851261700001918985429846269414760731099242709618533410330126;
    uint256 constant IC21y = 6470347777373269813521404122121303972412720219117864013617737535284191109005;
    
    uint256 constant IC22x = 18666857306414511177520257461525081058581820410049107313642692848130088407572;
    uint256 constant IC22y = 18512487782985774061891369520561194707176291895276651860521417877415064856135;
    
    uint256 constant IC23x = 20586601892183072329449038192872861373038634968153174343656985079964793730512;
    uint256 constant IC23y = 168154202160429441739344186959042414260564001839170562727970597108043428909;
    
    uint256 constant IC24x = 6147249023783259981276614659226361258073849557574201934498328677029140288311;
    uint256 constant IC24y = 8470224534047757992783808558578527869880815314827493169006162237868319061629;
    
    uint256 constant IC25x = 18186710679151771988586766382385856584867616600508504449305838589547917663445;
    uint256 constant IC25y = 8568924675305432143665604888717287580552781636713206347816791620602047040281;
    
    uint256 constant IC26x = 2937728593037664177825893419033606953757857939807497860912530839927766757809;
    uint256 constant IC26y = 14501697050994491876741748491331663954625620614431367260500360124631226376792;
    
    uint256 constant IC27x = 1783351603844888105139473979988632016838840326955021972788645071706132245449;
    uint256 constant IC27y = 7453161179784007383243471587192485822454641203418704532892911775693869291763;
    
    uint256 constant IC28x = 2811470308893431044072218963584144345830569692080264722803620656862769103849;
    uint256 constant IC28y = 3721766990601042883756985548496440602798600907980060481101597745814604257768;
    
    uint256 constant IC29x = 9357223620813522695792171909641683508507506669441805413414708346548459578220;
    uint256 constant IC29y = 17156978917737226931987357188775129163949448853339801114754008969711966493998;
    
    uint256 constant IC30x = 3137867433790644152729472002395166097218637069765647278721240899914224211887;
    uint256 constant IC30y = 7360695263876506126518036521861170562218813464749640729668282584390525430475;
    
    uint256 constant IC31x = 19203228499923368492828817237572109904207223845381144305070858738721516039442;
    uint256 constant IC31y = 13718913647402785316815925803001788824689895748740024220827031328877255712589;
    
    uint256 constant IC32x = 4905325056041069160533684923883923248988527336151697724592753345018186198859;
    uint256 constant IC32y = 735975056347540172914905016736429697935804469609761323804815271915590428270;
    
    uint256 constant IC33x = 17414727320079766806268886078203757093293405200201016256369559821752071749382;
    uint256 constant IC33y = 9159031135378827236629851903304242250675014137069473830254722598744541617719;
    
    uint256 constant IC34x = 17935394486271732807589737226763616260992977203871910339078387493883197339429;
    uint256 constant IC34y = 19865445290609736309828240466596393408798398722728302705647504731213997041527;
    
    uint256 constant IC35x = 1806668774163228877222116471325698946582784286913847146646377153264930901502;
    uint256 constant IC35y = 3047491746511844465129847004374668477402707863151811726064696597758268303310;
    
    uint256 constant IC36x = 19664538668462333671404239189522837177847827416780770431730043578752463819581;
    uint256 constant IC36y = 6918901439262475644547639869355648183282000194348751764985345789731298811865;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[36] calldata _pubSignals) public view returns (bool) {
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
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
